# nRF52832 RTT 实时监控与可视化

本项目通过 **J-Link** 的 RTT（Real-Time Transfer）接口读取 **nRF52832** 的调试口数据，用于实时采集和可视化 **J30D 热流传感器**（JonDeTech）的测量结果，包括 MCU/NTC 温度、目标温度 Tobj、环境温度 Ta、HF 电压与 HF 热流密度等关键指标。

## 功能特性

- **RTT 监听**：实时打印 RTT 通道 0 数据，支持时间戳，并自动保存日志文件
- **实时可视化**：通过 SSE（Server-Sent Events）将解析后的测量数据实时推送到浏览器
- **Web 看板**：基于 ECharts 的响应式仪表盘，展示关键指标卡片与实时趋势曲线
- **智能解析**：自动解析 RTT 文本流中的测量记录（温度、电压、通量、状态寄存器等）
- **优雅退出**：支持 Ctrl+C 安全断开 J-Link 与清理资源

## 项目结构

```
d:\hf-flux\
├── rtt_monitor.py      # RTT 命令行监听工具（实时打印 + 日志保存）
├── rtt_server.py       # RTT 实时可视化服务（SSE + 静态页面托管）
├── index.html          # Web 前端看板（ECharts 实时曲线）
├── echarts.min.js      # ECharts 前端图表库
└── j30d_datasheet.txt  # J30D 传感器技术手册（参考）
```

## 环境要求

- **Python 3.x**（需支持 `pylink` 库）
- **pylink**：`pip install pylink`
- **J-Link**：已安装 SEGGER J-Link 软件，DLL 默认路径为 `e:\SEGGER\JLink\JLink_x64.dll`（可在脚本头部修改 `JLINK_DLL` 常量）
- **硬件**：nRF52832 开发板 + J-Link 调试器（SWD 接口）

## 快速开始

### 1. 安装依赖

```bash
pip install pylink
```

> 国内网络环境建议使用镜像加速：
>
> ```bash
> pip install pylink -i https://pypi.tuna.tsinghua.edu.cn/simple
> ```

### 2. 使用命令行监听工具

实时打印 RTT 数据并保存日志：

```bash
# 默认参数（nRF52832_xxAA，SWD 4000kHz）
python rtt_monitor.py

# 指定芯片型号、J-Link 序列号与速度
python rtt_monitor.py --chip nRF52832_xxAB --sn 69730345 --speed 4000

# 关闭时间戳与日志保存
python rtt_monitor.py --no-timestamp --no-log

# CPU 处于停止/崩溃状态时，连接后自动复位并继续运行
python rtt_monitor.py --reset
```

参数说明：

| 参数               | 默认值            | 说明                     |
| ------------------ | ----------------- | ------------------------ |
| `--chip`         | `nRF52832_xxAA` | 芯片型号                 |
| `--sn`           | 自动              | J-Link 序列号            |
| `--speed`        | `4000`          | SWD 速度（kHz）          |
| `--no-timestamp` | 关闭              | 不打印时间戳             |
| `--no-log`       | 关闭              | 不保存日志文件           |
| `--reset`        | 关闭              | 连接后复位芯片并继续运行 |

日志默认保存到脚本同目录下的 `rtt_log.txt`（追加模式，带会话开始标记）。

### 3. 启动实时可视化服务

```bash
python rtt_server.py [--port 8765] [--sn 69730345] [--chip nRF52832_xxAA] [--speed 4000]
```

启动后浏览器访问：

```
http://localhost:8765
```

服务端会：

1. 后台线程通过 J-Link 读取 RTT 数据
2. 解析出完整的测量记录
3. 通过 SSE（`/events`）实时推送到前端
4. 前端使用 ECharts 绘制实时趋势曲线

## Web 看板说明

看板展示以下指标：

| 指标          | 单位   | 说明                 |
| ------------- | ------ | -------------------- |
| MCU 温度      | °C    | 芯片温度             |
| NTC 温度      | °C    | NTC 传感器温度       |
| 目标温度 Tobj | °C    | DSP 处理后的目标温度 |
| 环境温度 Ta   | °C    | 传感器环境温度       |
| HF 电压       | mV     | 热流传感器电压       |
| HF 热流密度   | mW/cm² | 热流密度             |

图表区包含：

- **温度趋势**：MCU / NTC / Tobj / Ta 四条曲线
- **热流电压**：HF 电压实时曲线
- **热流密度**：HF 热流密度实时曲线

## Tobj 估算模型与 U 标定

J30D 在 **sleep 模式**下（热流 k/m 校准必需）DSP 不通过 IIC 输出 Tobj，因此目标温度由上位机按**热阻模型**估算：

```
Tobj ≈ Ta + Q / U

  Q : 热流密度 (mW/cm²)，来自固件
  Ta: 环境温度 (°C)，来自 J30D 内部温度传感器
  U : 等效热导率 (W/m²·K)，即 (mW/cm²)/°C × 10
```

**U 标定值（人体应用场景，默认 95）**：依据 JIRS30 核心体温报告（v1.1a）实验室标定 —— 在加热台模拟人体（36~39.5°C）+ 手腕佩戴条件下确定**全局 U = 9.5 mW/(cm²·°C) = 95 W/(m²·K)**，`Tcbt = Tskin + Q/U` 与参考温度相关系数 0.999。

调节方式：

| 方式         | 说明                                       |
| ------------ | ------------------------------------------ |
| 启动参数     | `python rtt_server.py --u 95`（默认 95）   |
| 页面滑块     | 拖动实时生效（20~200 W/(m²·K)）            |
| API          | `GET /set_u?value=95` / `GET /get_u`       |

> 注：U 为时变参数，个体差异（体脂、佩戴松紧）会引入偏差。报告采用 4 天滚动数据缓冲实时估算 U；本工具先用固定标定值 95，如需更高精度可参照报告方法做个体标定。

## 解析规则

服务端通过正则表达式解析 RTT 文本流，识别以下字段：

- `Measurement #N` — 测量序号（标记一条新记录）
- `MCU:` / `NTC:` — 温度（°C）
- `Tobj:` / `Ta:` — 目标温度与环境温度（°C）
- `HF voltage:` — HF 电压（mV）
- `HF flux:` — 热流密度（mW/m）
- `HF2 cal (ref):` — HF2 校准参考（mV）
- `Status reg:` — 状态寄存器（hex）

## 常见问题

**J-Link DLL 未找到**
请确认已安装 SEGGER J-Link 软件，并修改脚本头部 `JLINK_DLL` 常量为你的实际 DLL 路径。

**无法连接芯片 / CPU 停止**
CPU 处于停止或崩溃状态时，使用 `--reset` 参数（监听工具）或等待服务端自动复位。

**浏览器访问不到页面**
确认服务已启动，端口未被占用，使用 `http://localhost:<port>` 访问。
