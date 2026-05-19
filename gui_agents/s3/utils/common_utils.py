import re
import time
from io import BytesIO
from PIL import Image

from typing import Tuple, Dict

from gui_agents.s3.memory.procedural_memory import PROCEDURAL_MEMORY

import logging

logger = logging.getLogger("desktopenv.agent")


def _mask_secret(value: str | None) -> str:
    if not value:
        return "(empty)"
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _engine_diagnostics(agent) -> dict:
    engine = getattr(agent, "engine", None)
    if engine is None:
        return {
            "engine": "(unknown)",
            "model": "(unknown)",
            "base_url": "(unknown)",
            "api_key": "(unknown)",
        }
    return {
        "engine": engine.__class__.__name__,
        "model": getattr(engine, "model", "(unknown)"),
        "base_url": getattr(engine, "base_url", None) or "(default)",
        "api_key": _mask_secret(getattr(engine, "api_key", "")),
    }


def _exception_diagnostics(exc: Exception) -> dict:
    details = {
        "type": type(exc).__name__,
        "message": str(exc),
        "status_code": getattr(exc, "status_code", None),
        "request_id": getattr(exc, "request_id", None),
    }
    response = getattr(exc, "response", None)
    if response is not None:
        details["status_code"] = details["status_code"] or getattr(
            response, "status_code", None
        )
        try:
            details["response_text"] = response.text[:500]
        except Exception:
            pass
    body = getattr(exc, "body", None)
    if body:
        details["body"] = str(body)[:500]
    return details


def _diagnosis_hint(engine_info: dict, exc_info: dict) -> str:
    status = exc_info.get("status_code")
    base_url = str(engine_info.get("base_url", ""))
    engine_name = str(engine_info.get("engine", ""))
    if status == 502:
        if "127.0.0.1" in base_url or "localhost" in base_url:
            return (
                "HTTP 502 from the local grounding endpoint. Check the SSH tunnel, "
                "remote vLLM process, and .windows/agent_s_paramiko_tunnel*.log."
            )
        return (
            "HTTP 502 from the main model endpoint or its upstream provider. "
            "This is usually gateway overload/restart/timeout; retry or switch endpoint."
        )
    if status in (500, 503, 504):
        return "Server-side model endpoint error. Retry later and check provider/vLLM logs."
    if status == 401:
        return "Authentication failed. Check the API key configured for this endpoint."
    if status == 403:
        return "Permission denied. Check token scope, model access, or endpoint authorization."
    if status == 404:
        return "Endpoint/model not found. Check base_url and served model name."
    if status == 429:
        return "Rate limited. Reduce concurrency or wait before retrying."
    if "HuggingFace" in engine_name or "vLLM" in engine_name:
        return "Grounding model call failed. Check UI-TARS endpoint, tunnel, and remote vLLM logs."
    return "Main model call failed. Check model endpoint, API key, provider status, and network."


def _format_llm_error(agent, exc: Exception, attempt: int, max_retries: int) -> str:
    engine_info = _engine_diagnostics(agent)
    exc_info = _exception_diagnostics(exc)
    lines = [
        f"[LLM ERROR] Attempt {attempt}/{max_retries} failed",
        f"  Engine: {engine_info['engine']}",
        f"  Model: {engine_info['model']}",
        f"  Base URL: {engine_info['base_url']}",
        f"  API key: {engine_info['api_key']}",
        f"  Exception: {exc_info['type']}: {exc_info['message']}",
    ]
    if exc_info.get("status_code") is not None:
        lines.append(f"  HTTP status: {exc_info['status_code']}")
    if exc_info.get("request_id"):
        lines.append(f"  Request ID: {exc_info['request_id']}")
    if exc_info.get("body"):
        lines.append(f"  Error body: {exc_info['body']}")
    elif exc_info.get("response_text"):
        lines.append(f"  Response text: {exc_info['response_text']}")
    lines.append(f"  Hint: {_diagnosis_hint(engine_info, exc_info)}")
    return "\n".join(lines)


def _is_retryable_llm_error(exc: Exception) -> bool:
    status_code = _exception_diagnostics(exc).get("status_code")
    if status_code in (400, 401, 403, 404, 422):
        return False
    if status_code in (408, 409, 425, 429, 500, 502, 503, 504):
        return True
    return status_code is None


def create_pyautogui_code(agent, code: str, obs: Dict) -> str:
    """
    Attempts to evaluate the code into a pyautogui code snippet with grounded actions using the observation screenshot.

    Args:
        agent (ACI): The grounding agent to use for evaluation.
        code (str): The code string to evaluate.
        obs (Dict): The current observation containing the screenshot.

    Returns:
        exec_code (str): The pyautogui code to execute the grounded action.

    Raises:
        Exception: If there is an error in evaluating the code.
    """
    agent.assign_screenshot(obs)  # Necessary for grounding
    exec_code = eval(code)
    return exec_code


def call_llm_safe(
    agent, temperature: float = 0.0, use_thinking: bool = False, **kwargs
) -> str:
    # Retry if fails
    max_retries = 3  # Set the maximum number of retries
    attempt = 0
    response = ""
    setattr(agent, "_last_llm_error_non_retryable", False)
    while attempt < max_retries:
        try:
            response = agent.get_response(
                temperature=temperature, use_thinking=use_thinking, **kwargs
            )
            assert response is not None, "Response from agent should not be None"
            setattr(agent, "_last_llm_error_non_retryable", False)
            print("Response success!")
            break  # If successful, break out of the loop
        except Exception as e:
            attempt += 1
            print(_format_llm_error(agent, e, attempt, max_retries))
            if not _is_retryable_llm_error(e):
                setattr(agent, "_last_llm_error_non_retryable", True)
                print("[LLM ERROR] Non-retryable endpoint/model/config error; not retrying.")
                break
            if attempt == max_retries:
                print("[LLM ERROR] Max retries reached. Returning an empty response.")
            else:
                wait_s = min(2**attempt, 8)
                print(f"[LLM RETRY] Waiting {wait_s}s before retrying...")
                time.sleep(wait_s)
    return response if response is not None else ""


def call_llm_formatted(generator, format_checkers, **kwargs):
    """
    Calls the generator agent's LLM and ensures correct formatting.

    Args:
        generator (ACI): The generator agent to call.
        obs (Dict): The current observation containing the screenshot.
        format_checkers (Callable): Functions that take the response and return a tuple of (success, feedback).
        **kwargs: Additional keyword arguments for the LLM call.

    Returns:
        response (str): The formatted response from the generator agent.
    """
    max_retries = 3  # Set the maximum number of retries
    attempt = 0
    response = ""
    if kwargs.get("messages") is None:
        messages = (
            generator.messages.copy()
        )  # Copy messages to avoid modifying the original
    else:
        messages = kwargs["messages"]
        del kwargs["messages"]  # Remove messages from kwargs to avoid passing it twice
    while attempt < max_retries:
        response = call_llm_safe(generator, messages=messages, **kwargs)
        if getattr(generator, "_last_llm_error_non_retryable", False):
            break

        # Prepare feedback messages for incorrect formatting
        feedback_msgs = []
        for format_checker in format_checkers:
            success, feedback = format_checker(response)
            if not success:
                feedback_msgs.append(feedback)
        if not feedback_msgs:
            # logger.info(f"Response formatted correctly on attempt {attempt} for {generator.engine.model}")
            break
        logger.error(
            f"Response formatting error on attempt {attempt} for {generator.engine.model}. Response: {response} {', '.join(feedback_msgs)}"
        )
        messages.append(
            {
                "role": "assistant",
                "content": [{"type": "text", "text": response}],
            }
        )
        logger.info(f"Bad response: {response}")
        delimiter = "\n- "
        formatting_feedback = f"- {delimiter.join(feedback_msgs)}"
        messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": PROCEDURAL_MEMORY.FORMATTING_FEEDBACK_PROMPT.replace(
                            "FORMATTING_FEEDBACK", formatting_feedback
                        ),
                    }
                ],
            }
        )
        logger.info("Feedback:\n%s", formatting_feedback)

        attempt += 1
        if attempt == max_retries:
            logger.error(
                "Max retries reached when formatting response. Handling failure."
            )
        time.sleep(1.0)
    return response


def split_thinking_response(full_response: str) -> Tuple[str, str]:
    try:
        # Extract thoughts section
        thoughts = full_response.split("<thoughts>")[-1].split("</thoughts>")[0].strip()

        # Extract answer section
        answer = full_response.split("<answer>")[-1].split("</answer>")[0].strip()

        return answer, thoughts
    except Exception as e:
        return full_response, ""


def parse_code_from_string(input_string):
    """Parses a string to extract each line of code enclosed in triple backticks (```)

    Args:
        input_string (str): The input string containing code snippets.

    Returns:
        str: The last code snippet found in the input string, or an empty string if no code is found.
    """
    input_string = input_string.strip()

    # This regular expression will match both ```code``` and ```python code```
    # and capture the `code` part. It uses a non-greedy match for the content inside.
    pattern = r"```(?:\w+\s+)?(.*?)```"

    # Find all non-overlapping matches in the string
    matches = re.findall(pattern, input_string, re.DOTALL)
    if len(matches) == 0:
        # return []
        return ""
    relevant_code = matches[
        -1
    ]  # We only care about the last match given it is the grounded action
    return relevant_code


def extract_agent_functions(code):
    """Extracts all agent function calls from the given code.

    Args:
        code (str): The code string to search for agent function calls.

    Returns:
        list: A list of all agent function calls found in the code.
    """
    pattern = r"(agent\.\w+\(\s*.*\))"  # Matches
    return re.findall(pattern, code)


def compress_image(image_bytes: bytes = None, image: Image = None) -> bytes:
    """Compresses an image represented as bytes.

    Compression involves resizing image into half its original size and saving to webp format.

    Args:
        image_bytes (bytes): The image data to compress.

    Returns:
        bytes: The compressed image data.
    """
    if not image:
        image = Image.open(BytesIO(image_bytes))
    output = BytesIO()
    image.save(output, format="WEBP")
    compressed_image_bytes = output.getvalue()
    return compressed_image_bytes
