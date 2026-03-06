import socket
import struct
import time
import argparse
import sys
from typing import List

def parse_hex_bytes(hex_str: str, expected_length: int) -> bytes:
    """
    解析十六进制字符串为字节数据
    
    Args:
        hex_str: 十六进制字符串，可以包含空格
        expected_length: 期望的字节长度
    
    Returns:
        转换后的字节数据
    
    Raises:
        ValueError: 如果输入格式不正确或长度不匹配
    """
    # 移除所有空格
    hex_str = hex_str.replace(' ', '')
    
    # 检查字符串长度是否正确
    if len(hex_str) != expected_length * 2:
        raise ValueError(f"十六进制字符串长度不正确，应为{expected_length * 2}个字符，实际为{len(hex_str)}个字符")
    
    # 检查是否都是有效的十六进制字符
    if not all(c in '0123456789ABCDEFabcdef' for c in hex_str):
        raise ValueError("包含无效的十六进制字符")
    
    # 转换为字节
    try:
        return bytes.fromhex(hex_str)
    except ValueError as e:
        raise ValueError(f"转换十六进制字符串失败: {e}")

def create_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器"""
    parser = argparse.ArgumentParser(
        description='发送CAN报文到指定设备（通过UDP）',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  %(prog)s -i 192.168.1.100 -p 61206 -t 100 -c 18FF0102 -d "11 22 33 44 55 66 77 88"
  %(prog)s --ip 192.168.1.100 --port 61206 --interval 100 --can-id 0x18FF0102 --data "1122334455667788"
        '''
    )
    
    parser.add_argument('-i', '--ip', 
                       required=False,
                       default='192.168.1.206',
                       help='目标设备IP地址')
    
    parser.add_argument('-p', '--port',
                       type=int,
                       required=False,
                       default=61206,
                       help='目标设备UDP端口')
    
    parser.add_argument('-t', '--interval',
                       type=int,
                       required=False,
                       default=20,
                       help='发送间隔（毫秒）')
    
    parser.add_argument('-c', '--can-id',
                       required=True,
                       help='CAN ID（4字节十六进制，例如：18FF0102）')
    
    parser.add_argument('-d', '--data',
                       required=False,
                       default='11 22 33 44 55 66 77 88',    
                       help='8字节数据（十六进制，可以包含空格）')
    
    return parser

def main():
    # 解析命令行参数
    parser = create_parser()
    args = parser.parse_args()
    
    try:
        # 解析CAN ID
        if args.can_id.startswith('0x'):
            args.can_id = args.can_id[2:]
        can_id_bytes = parse_hex_bytes(args.can_id, 4)
        
        # 解析数据
        data_bytes = parse_hex_bytes(args.data, 8)
        
        # 创建UDP socket
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        
        print(f"\n开始发送CAN报文:")
        print(f"目标地址: {args.ip}:{args.port}")
        print(f"CAN ID: 0x{args.can_id}")
        print(f"数据: {' '.join([f'{b:02X}' for b in data_bytes])}")
        print(f"间隔: {args.interval}ms")
        print("\n按Ctrl+C停止发送...\n")
        
        # 计数器
        count = 0
        start_time = time.time()
        
        while True:
            # 组合12字节的UDP数据
            udp_data = can_id_bytes + data_bytes
            
            # 发送数据
            sock.sendto(udp_data, (args.ip, args.port))
            
            # 更新计数器和统计
            count += 1
            elapsed_time = time.time() - start_time
            rate = count / elapsed_time if elapsed_time > 0 else 0
            
            # 打印状态（覆盖上一行）
            print(f"\r已发送: {count} 帧  运行时间: {elapsed_time:.1f}秒  发送速率: {rate:.1f}帧/秒", end='')
            
            # 等待指定的间隔时间
            time.sleep(args.interval / 1000)
            
    except ValueError as e:
        print(f"\n错误: {e}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\n程序已停止")
    except Exception as e:
        print(f"\n发送错误: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        if 'sock' in locals():
            sock.close()

i
    
