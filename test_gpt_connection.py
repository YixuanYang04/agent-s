#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
GPT连接稳定性测试脚本
测试GPT API连接的稳定性和响应时间
"""

import time
import sys
import os
from datetime import datetime
from typing import List, Dict, Tuple

# 设置输出编码为UTF-8（Windows兼容）
if sys.platform == 'win32':
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except AttributeError:
        # Python < 3.7
        import codecs
        sys.stdout = codecs.getwriter('utf-8')(sys.stdout.buffer, 'strict')
        sys.stderr = codecs.getwriter('utf-8')(sys.stderr.buffer, 'strict')

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from gui_agents.s3.core.engine import LMMEngineOpenAI
    from openai import APIConnectionError, APIError, RateLimitError
except ImportError as e:
    print(f"导入错误: {e}")
    print("请确保已安装所需依赖: pip install openai")
    sys.exit(1)


class GPTConnectionTester:
    """GPT连接测试器"""
    
    def __init__(self, base_url: str, api_key: str, model: str):
        """
        初始化测试器
        
        Args:
            base_url: API基础URL
            api_key: API密钥
            model: 模型名称
        """
        self.base_url = base_url
        self.api_key = api_key
        self.model = model
        self.results: List[Dict] = []
        
    def test_single_request(self, test_num: int) -> Dict:
        """
        执行单次请求测试
        
        Args:
            test_num: 测试编号
            
        Returns:
            测试结果字典
        """
        result = {
            'test_num': test_num,
            'success': False,
            'response_time': 0,
            'error': None,
            'response_length': 0,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        }
        
        try:
            # 创建引擎实例
            engine = LMMEngineOpenAI(
                base_url=self.base_url,
                api_key=self.api_key,
                model=self.model
            )
            
            # 准备测试消息
            test_message = [
                {
                    "role": "user",
                    "content": "请回复'连接测试成功'，仅此一句话即可。"
                }
            ]
            
            # 记录开始时间
            start_time = time.time()
            
            # 发送请求
            response = engine.generate(
                messages=test_message,
                temperature=0.0,
                max_new_tokens=50
            )
            
            # 计算响应时间
            response_time = time.time() - start_time
            
            # 记录成功结果
            result['success'] = True
            result['response_time'] = round(response_time, 2)
            result['response_length'] = len(response) if response else 0
            
            print(f"[OK] 测试 #{test_num}: 成功 (响应时间: {response_time:.2f}秒)")
            
        except APIConnectionError as e:
            result['error'] = f"连接错误: {str(e)}"
            print(f"[FAIL] 测试 #{test_num}: 连接错误 - {str(e)}")
            
        except APIError as e:
            result['error'] = f"API错误: {str(e)}"
            print(f"[FAIL] 测试 #{test_num}: API错误 - {str(e)}")
            
        except RateLimitError as e:
            result['error'] = f"速率限制: {str(e)}"
            print(f"[FAIL] 测试 #{test_num}: 速率限制 - {str(e)}")
            
        except Exception as e:
            result['error'] = f"未知错误: {type(e).__name__}: {str(e)}"
            print(f"[FAIL] 测试 #{test_num}: 未知错误 - {type(e).__name__}: {str(e)}")
        
        self.results.append(result)
        return result
    
    def run_tests(self, num_tests: int = 10, interval: float = 1.0):
        """
        运行多次测试
        
        Args:
            num_tests: 测试次数
            interval: 每次测试之间的间隔（秒）
        """
        print("=" * 60)
        print("GPT连接稳定性测试")
        print("=" * 60)
        print(f"API URL: {self.base_url}")
        print(f"模型: {self.model}")
        print(f"测试次数: {num_tests}")
        print(f"测试间隔: {interval}秒")
        print("=" * 60)
        print()
        
        for i in range(1, num_tests + 1):
            self.test_single_request(i)
            if i < num_tests:
                time.sleep(interval)
        
        print()
        self.print_statistics()
    
    def print_statistics(self):
        """打印统计信息"""
        total_tests = len(self.results)
        successful_tests = sum(1 for r in self.results if r['success'])
        failed_tests = total_tests - successful_tests
        
        success_rate = (successful_tests / total_tests * 100) if total_tests > 0 else 0
        
        successful_results = [r for r in self.results if r['success']]
        
        if successful_results:
            response_times = [r['response_time'] for r in successful_results]
            avg_response_time = sum(response_times) / len(response_times)
            min_response_time = min(response_times)
            max_response_time = max(response_times)
        else:
            avg_response_time = 0
            min_response_time = 0
            max_response_time = 0
        
        print("=" * 60)
        print("测试统计结果")
        print("=" * 60)
        print(f"总测试次数: {total_tests}")
        print(f"成功次数: {successful_tests}")
        print(f"失败次数: {failed_tests}")
        print(f"成功率: {success_rate:.2f}%")
        print()
        
        if successful_results:
            print("响应时间统计:")
            print(f"  平均响应时间: {avg_response_time:.2f}秒")
            print(f"  最快响应时间: {min_response_time:.2f}秒")
            print(f"  最慢响应时间: {max_response_time:.2f}秒")
            print()
        
        # 错误统计
        if failed_tests > 0:
            print("错误详情:")
            error_types = {}
            for r in self.results:
                if not r['success'] and r['error']:
                    error_type = r['error'].split(':')[0]
                    error_types[error_type] = error_types.get(error_type, 0) + 1
            
            for error_type, count in error_types.items():
                print(f"  {error_type}: {count}次")
            print()
        
        # 稳定性评估
        print("稳定性评估:")
        if success_rate >= 95:
            stability = "优秀 [OK]"
        elif success_rate >= 80:
            stability = "良好 [OK]"
        elif success_rate >= 60:
            stability = "一般 [WARN]"
        else:
            stability = "较差 [FAIL]"
        
        print(f"  连接稳定性: {stability}")
        
        if successful_results and avg_response_time > 0:
            if avg_response_time < 2:
                speed = "快速 [OK]"
            elif avg_response_time < 5:
                speed = "正常"
            else:
                speed = "较慢 [WARN]"
            print(f"  响应速度: {speed}")
        
        print("=" * 60)


def main():
    """主函数"""
    try:
        from dotenv import load_dotenv
    except ImportError:
        print("请先安装 python-dotenv: pip install python-dotenv")
        sys.exit(1)

    load_dotenv(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
        override=True,
        encoding="utf-8-sig",
    )

    BASE_URL = (
        os.getenv("AGENT_S_MODEL_URL")
        or os.getenv("OPENAI_BASE_URL")
        or "https://aihubmix.com/v1"
    )
    API_KEY = os.getenv("AGENT_S_MODEL_API_KEY") or os.getenv("OPENAI_API_KEY")
    MODEL = os.getenv("AGENT_S_MODEL") or os.getenv("ARCHIVE_LLM_MODEL") or "gpt-5.4"

    if not API_KEY:
        print("缺少主模型 API Key：请在 .env 中设置 AGENT_S_MODEL_API_KEY 或 OPENAI_API_KEY")
        sys.exit(1)
    
    # 测试参数
    NUM_TESTS = 10  # 测试次数
    INTERVAL = 1.0  # 每次测试间隔（秒）
    
    # 创建测试器并运行测试
    tester = GPTConnectionTester(
        base_url=BASE_URL,
        api_key=API_KEY,
        model=MODEL
    )
    
    try:
        tester.run_tests(num_tests=NUM_TESTS, interval=INTERVAL)
    except KeyboardInterrupt:
        print("\n\n测试被用户中断")
        if tester.results:
            print("\n已完成的测试结果:")
            tester.print_statistics()
    except Exception as e:
        print(f"\n测试过程中发生错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
