# -*- coding: utf-8 -*-
"""
nRF52832 RTT 监听脚本
=====================
通过 J-Link 读取已使能 RTT 的 nRF52832 调试口数据。
- 默认连接 nRF52832_xxAA，SWD 接口，4000 kHz
- 实时打印 RTT 通道 0 数据（带时间戳）
- 自动保存到 rtt_log.txt
- Ctrl+C 安全退出

用法:
    python rtt_monitor.py                    # 默认参数
    python rtt_monitor.py --chip nRF52832_xxAB
    python rtt_monitor.py --sn 69730345 --speed 4000 --no-timestamp
"""

import argparse
import ctypes
import os
import signal
import sys
import time
from datetime import datetime

# J-Link DLL 路径（你的安装位置）
JLINK_DLL = r'e:\SEGGER\JLink\JLink_x64.dll'
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rtt_log.txt')

import pylink
import pylink.library as library
from pylink import enums


class RTTMonitor:
    def __init__(self, chip='nRF52832_xxAA', serial_no=None, speed=4000,
                 timestamp=True, log=True):
        self.chip = chip
        self.serial_no = serial_no
        self.speed = speed
        self.timestamp = timestamp
        self.log = log
        self.jlink = None
        self.running = False

    def connect(self, reset=False):
        """连接 J-Link 并启动 RTT。

        Args:
            reset (bool): 复位芯片并继续运行（用于 CPU 处于停止/崩溃状态时）
        """
        lib = library.Library(dllpath=JLINK_DLL)
        self.jlink = pylink.JLink(lib=lib)
        print(f'[INFO] J-Link DLL 版本: {self.jlink.version}')

        if self.serial_no:
            self.jlink.open(self.serial_no)
        else:
            self.jlink.open()
        print(f'[INFO] J-Link 已连接, SN: {self.jlink.serial_number}, '
              f'固件: {self.jlink.firmware_version}')

        # 设置 SWD 接口
        self.jlink.set_tif(enums.JLinkInterfaces.SWD)
        print(f'[INFO] 接口: SWD, 速度: {self.speed} kHz')

        # 连接芯片
        self.jlink.connect(self.chip)
        print(f'[INFO] 已连接芯片: {self.chip}')

        if reset:
            print('[INFO] 复位芯片并继续运行...')
            self.jlink.reset(ms=50, halt=False)
            time.sleep(1)
            print(f'[INFO] 复位后 CPU 停止状态: {self.jlink.halted()}')

        # 启动 RTT
        self.jlink.rtt_start()
        print('[INFO] RTT 已启动, 监听通道 0 ...')
        print('[INFO] 按 Ctrl+C 停止监听\n' + '=' * 60)

    def read_loop(self):
        """持续读取 RTT 通道 0 数据。"""
        self.running = True
        f = None
        if self.log:
            f = open(LOG_FILE, 'a', encoding='utf-8')
            f.write(f'\n===== 会话开始 {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} =====\n')

        try:
            while self.running:
                data = self.jlink.rtt_read(0, 2048)
                if data:
                    text = ''.join(chr(b) for b in data)
                    if self.timestamp:
                        ts = datetime.now().strftime('%H:%M:%S.%f')[:-3]
                        text = f'[{ts}] {text}'
                    print(text, end='', flush=True)
                    if f:
                        f.write(text)
                        f.flush()
                else:
                    time.sleep(0.002)
        finally:
            if f:
                f.close()

    def cleanup(self):
        """停止 RTT 并断开连接。"""
        self.running = False
        if self.jlink:
            try:
                self.jlink.rtt_stop()
            except Exception:
                pass
            try:
                self.jlink.close()
            except Exception:
                pass
            print('\n[INFO] 已断开 J-Link 连接')


def main():
    parser = argparse.ArgumentParser(description='nRF52832 RTT 监听工具')
    parser.add_argument('--chip', default='nRF52832_xxAA',
                        help='芯片型号 (默认 nRF52832_xxAA)')
    parser.add_argument('--sn', default=None, help='J-Link 序列号 (默认自动)')
    parser.add_argument('--speed', type=int, default=4000, help='SWD 速度 kHz (默认 4000)')
    parser.add_argument('--no-timestamp', action='store_true', help='不打印时间戳')
    parser.add_argument('--no-log', action='store_true', help='不保存日志文件')
    parser.add_argument('--reset', action='store_true',
                        help='连接后复位芯片并继续运行（CPU 停止/崩溃时使用）')
    args = parser.parse_args()

    monitor = RTTMonitor(
        chip=args.chip,
        serial_no=args.sn,
        speed=args.speed,
        timestamp=not args.no_timestamp,
        log=not args.no_log,
    )

    # Ctrl+C 优雅退出
    def handler(sig, frame):
        monitor.cleanup()
        sys.exit(0)
    signal.signal(signal.SIGINT, handler)

    try:
        monitor.connect(reset=args.reset)
        monitor.read_loop()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        print(f'\n[ERROR] {e}')
        monitor.cleanup()
        sys.exit(1)


if __name__ == '__main__':
    main()
