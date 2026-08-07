# -*- coding: utf-8 -*-
"""
nRF52832 RTT 实时可视化服务
============================
1. 通过 J-Link 读取 nRF52832 的 RTT 数据
2. 解析关键测量数据（MCU/NTC 温度、Tobj、Ta、HF 电压/通量）
3. 通过 SSE (Server-Sent Events) 实时推送到浏览器
4. 同时提供前端页面 (index.html)

用法:
    python rtt_server.py [--port 8765] [--sn 69730345] [--chip nRF52832_xxAA]
"""

import argparse
import json
import os
import queue
import re
import signal
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pylink
import pylink.library as library
from pylink import enums

JLINK_DLL = r'e:\SEGGER\JLink\JLink_x64.dll'
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ============================================================================
# Tobj 估算模型（早期样品无 V-T 表时使用）
# ----------------------------------------------------------------------------
# 热阻模型: Q = U · (Tobj - Ta)  ⟹  Tobj = Ta + Q / U
#   Q   : 热流密度 (mW/cm²) — 来自固件
#   Ta  : 环境温度 (°C) — 来自 J30D 内部温度传感器
#   U   : 等效热导率 (W/m²·K) — 需按安装场景标定, 可通过 --u 或页面调节
# 单位换算: 1 mW/cm² = 10 W/m²
# ============================================================================
DEFAULT_U = 50.0   # 默认等效热导率 W/(m²·K)
U_VALUE = DEFAULT_U
U_LOCK = threading.Lock()

def set_u(value):
    """更新等效热导率 U (线程安全)。"""
    global U_VALUE
    with U_LOCK:
        U_VALUE = float(value)

def get_u():
    with U_LOCK:
        return U_VALUE

def estimate_tobj(ta, q_mw_cm2):
    """由 Ta 和热流 Q (mW/cm²) 估算目标温度 Tobj (°C)。"""
    if ta is None or q_mw_cm2 is None:
        return None
    q_w_m2 = q_mw_cm2 * 10.0        # mW/cm² → W/m²
    u = get_u()
    if u <= 0:
        return None
    return ta + q_w_m2 / u

# 数据队列：解析后的每条测量记录
DATA_QUEUE = queue.Queue(maxsize=100)
SHUTDOWN = threading.Event()


class RTTParser:
    """解析 RTT 文本流，提取测量记录。"""

    # 测量开始标记
    RE_MEASURE = re.compile(r'Measurement #(\d+)')
    RE_MCU_TEMP = re.compile(r'MCU:\s*([\d.]+)\s*C')
    RE_NTC_TEMP = re.compile(r'NTC:\s*([\d.]+)\s*C')
    RE_TOBJ = re.compile(r'Tobj:\s*([\d.]+)\s*C')
    RE_TA = re.compile(r'Ta:\s*([\d.]+)\s*C')
    RE_HF_VOLT = re.compile(r'HF voltage:\s*([\d.]+)\s*mV')
    RE_HF_FLUX = re.compile(r'HF flux:\s*([\d.]+)\s*mW/(?:m|cm)\^?2?')
    RE_HF2_REF = re.compile(r'HF2 cal \(ref\):\s*([\d.]+)\s*mV')
    RE_STATUS = re.compile(r'Status reg:\s*0x([0-9A-Fa-f]+)')

    def __init__(self):
        self.buffer = ''
        self.current = None

    def _new_record(self, measure_no):
        return {
            'ts': datetime.now().strftime('%H:%M:%S'),
            'measure': measure_no,
            'mcu': None, 'ntc': None,
            'tobj': None, 'tobj_est': None, 'ta': None,
            'hf_volt': None, 'hf_flux': None, 'hf2_ref': None,
            'status': None,
        }

    def _finalize(self, rec):
        """记录完成前的收尾：计算 Tobj 估算值。"""
        rec['tobj_est'] = estimate_tobj(rec['ta'], rec['hf_flux'])

    def feed(self, text):
        """喂入一段 RTT 文本，返回解析出的完整记录列表。"""
        records = []
        self.buffer += text

        for line in self.buffer.splitlines():
            line = line.strip()

            m = self.RE_MEASURE.search(line)
            if m:
                # 上一记录完成，先提交（含 Tobj 估算）
                if self.current is not None:
                    self._finalize(self.current)
                    records.append(self.current)
                self.current = self._new_record(int(m.group(1)))
                continue

            if self.current is None:
                continue

            for attr, regex in [
                ('mcu', self.RE_MCU_TEMP), ('ntc', self.RE_NTC_TEMP),
                ('tobj', self.RE_TOBJ), ('ta', self.RE_TA),
                ('hf_volt', self.RE_HF_VOLT), ('hf_flux', self.RE_HF_FLUX),
                ('hf2_ref', self.RE_HF2_REF),
            ]:
                mm = regex.search(line)
                if mm:
                    self.current[attr] = float(mm.group(1))
                    break

            sm = self.RE_STATUS.search(line)
            if sm:
                self.current['status'] = int(sm.group(1), 16)

        # 保留最后一行未完成的部分
        lines = self.buffer.splitlines()
        self.buffer = lines[-1] if lines else ''
        return records

    def flush(self):
        """返回并清空当前未完成的记录。"""
        rec = self.current
        self.current = None
        return [rec] if rec else []


class JLinkReader(threading.Thread):
    """后台线程：连接 J-Link 读取 RTT 并解析。"""

    def __init__(self, chip, serial_no, speed):
        super().__init__(daemon=True)
        self.chip = chip
        self.serial_no = serial_no
        self.speed = speed
        self.parser = RTTParser()

    def run(self):
        try:
            lib = library.Library(dllpath=JLINK_DLL)
            jlink = pylink.JLink(lib=lib)
            print(f'[服务] J-Link DLL: {jlink.version}')
            jlink.open(self.serial_no) if self.serial_no else jlink.open()
            print(f'[服务] 已连接 J-Link SN: {jlink.serial_number}')
            jlink.set_tif(enums.JLinkInterfaces.SWD)
            jlink.set_speed(self.speed)
            jlink.connect(self.chip)
            print(f'[服务] 已连接芯片: {self.chip}')

            # 如 CPU 处于停止状态，复位并运行
            if jlink.halted():
                print('[服务] CPU 停止，复位中...')
                jlink.reset(ms=50, halt=False)
                time.sleep(0.5)

            jlink.rtt_start()
            print('[服务] RTT 已启动，开始监听...')

            while not SHUTDOWN.is_set():
                try:
                    data = jlink.rtt_read(0, 2048)
                except pylink.errors.JLinkException:
                    # 可能暂时性错误，重试
                    time.sleep(0.1)
                    continue
                if data:
                    text = ''.join(chr(b) for b in data)
                    for rec in self.parser.feed(text):
                        try:
                            DATA_QUEUE.put_nowait(rec)
                        except queue.Full:
                            try:
                                DATA_QUEUE.get_nowait()
                            except queue.Empty:
                                pass
                            DATA_QUEUE.put_nowait(rec)
                else:
                    time.sleep(0.002)
        except Exception as e:
            print(f'[服务] 采集线程错误: {e}')
            # 尝试把错误推给前端
            try:
                DATA_QUEUE.put_nowait({'error': str(e)})
            except queue.Full:
                pass


class SSEHandler(BaseHTTPRequestHandler):
    """HTTP + SSE 请求处理。"""

    def log_message(self, fmt, *args):
        pass  # 静默访问日志

    def _send_headers(self, content_type, extra=None):
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Access-Control-Allow-Origin', '*')
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()

    def do_GET(self):
        if self.path == '/' or self.path == '/index.html':
            self._serve_file('index.html', 'text/html; charset=utf-8')
        elif self.path == '/echarts.min.js':
            self._serve_file('echarts.min.js', 'application/javascript')
        elif self.path == '/events':
            self._serve_sse()
        elif self.path.startswith('/set_u?'):
            self._handle_set_u()
        elif self.path == '/get_u':
            self._handle_get_u()
        else:
            self.send_error(404)

    def _handle_set_u(self):
        """调节等效热导率 U: /set_u?value=50"""
        from urllib.parse import urlparse, parse_qs
        qs = parse_qs(urlparse(self.path).query)
        try:
            v = float(qs.get('value', ['50'])[0])
            set_u(v)
            self._send_headers('application/json')
            self.wfile.write(json.dumps({'ok': True, 'u': get_u()}).encode())
        except (ValueError, TypeError):
            self._send_headers('application/json')
            self.wfile.write(json.dumps({'ok': False, 'error': 'invalid u'}).encode())

    def _handle_get_u(self):
        self._send_headers('application/json')
        self.wfile.write(json.dumps({'u': get_u()}).encode())

    def _serve_file(self, fname, content_type):
        path = os.path.join(BASE_DIR, fname)
        try:
            with open(path, 'rb') as f:
                content = f.read()
            self._send_headers(content_type)
            self.wfile.write(content)
        except FileNotFoundError:
            self.send_error(404)

    def _serve_sse(self):
        self._send_headers('text/event-stream', {
            'Connection': 'keep-alive',
            'X-Accel-Buffering': 'no',
        })
        try:
            while not SHUTDOWN.is_set():
                try:
                    rec = DATA_QUEUE.get(timeout=1)
                except queue.Empty:
                    # 心跳，保持连接
                    self.wfile.write(b': keepalive\n\n')
                    self.wfile.flush()
                    continue
                msg = f'data: {json.dumps(rec, ensure_ascii=False)}\n\n'
                self.wfile.write(msg.encode('utf-8'))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass


def main():
    parser = argparse.ArgumentParser(description='nRF52832 RTT 实时可视化服务')
    parser.add_argument('--port', type=int, default=8765, help='服务端口 (默认 8765)')
    parser.add_argument('--chip', default='nRF52832_xxAA', help='芯片型号')
    parser.add_argument('--sn', default='69730345', help='J-Link 序列号')
    parser.add_argument('--speed', type=int, default=4000, help='SWD 速度 kHz')
    parser.add_argument('--u', type=float, default=DEFAULT_U,
                        help=f'等效热导率 U W/(m²·K), 用于 Tobj=Ta+Q/U 估算 (默认 {DEFAULT_U})')
    args = parser.parse_args()

    set_u(args.u)
    print(f'[服务] Tobj 估算参数: U = {get_u():.1f} W/(m²·K)')

    def shutdown_handler(sig, frame):
        print('\n[服务] 正在退出...')
        SHUTDOWN.set()
        sys.exit(0)
    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    reader = JLinkReader(args.chip, args.sn, args.speed)
    reader.start()

    server = ThreadingHTTPServer(('0.0.0.0', args.port), SSEHandler)
    print(f'\n[服务] 实时可视化已启动!')
    print(f'[服务] 浏览器访问: http://localhost:{args.port}')
    print(f'[服务] 按 Ctrl+C 停止\n')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        SHUTDOWN.set()
        server.shutdown()


if __name__ == '__main__':
    main()
