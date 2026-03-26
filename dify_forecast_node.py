#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dify代码节点 - 时序预测（保持波动特征）
输出格式: {"result": "预测值1,预测值2,预测值3,..."}

核心策略：
  高波动数据 → 去趋势 + 残差周期复现（无累积漂移）
  低波动数据 → Holt-Winters指数平滑
  单调数据   → 阻尼线性外推
"""

import numpy as np
import warnings
from collections import Counter

warnings.filterwarnings('ignore')


# ======================== 周期识别 ========================
def detect_period(data: np.ndarray, max_period: int = None) -> int:
    """结合自相关和FFT识别主要周期"""
    from scipy import signal as scipy_signal

    n = len(data)
    if n < 4:
        return 2
    if max_period is None:
        max_period = min(n // 2, 60)

    candidates = []

    # 自相关
    if n > 10:
        try:
            x = data - np.mean(data)
            ac = np.correlate(x, x, mode='full')[n - 1:]
            if ac[0] != 0:
                ac = ac / ac[0]
            for lag in range(2, min(max_period + 1, len(ac) - 1)):
                if ac[lag] > 0.25 and ac[lag] > ac[lag - 1] and ac[lag] > ac[lag + 1]:
                    candidates.append(lag)
        except Exception:
            pass

    # FFT
    if n > 20:
        try:
            detrended = data - np.linspace(data[0], data[-1], n)
            fft_vals = np.fft.rfft(detrended)
            fft_freqs = np.fft.rfftfreq(n)
            power = np.abs(fft_vals)
            power[0] = 0
            peaks, _ = scipy_signal.find_peaks(power, height=np.max(power) * 0.1)
            for idx in peaks:
                if fft_freqs[idx] > 0:
                    p = int(round(1.0 / fft_freqs[idx]))
                    if 2 <= p <= max_period:
                        candidates.append(p)
        except Exception:
            pass

    if not candidates:
        return max(2, min(max_period, n // 4))

    counts = Counter(candidates)
    return counts.most_common(1)[0][0]


# ======================== 趋势估计 ========================
def linear_trend(data: np.ndarray):
    """对全段数据做线性回归，返回 (slope, intercept)"""
    n = len(data)
    if n < 2:
        return 0.0, float(data[-1]) if n else 0.0
    x = np.arange(n, dtype=float)
    try:
        coeffs = np.polyfit(x, data, 1)
        return float(coeffs[0]), float(coeffs[1])
    except Exception:
        return 0.0, float(data[-1])


def recent_trend(data: np.ndarray, window: int = 10) -> float:
    """近期趋势斜率（每步平均变化量）"""
    n = len(data)
    w = min(window, n)
    if w < 2:
        return 0.0
    try:
        x = np.arange(w, dtype=float)
        return float(np.polyfit(x, data[-w:], 1)[0])
    except Exception:
        return 0.0


def is_monotonic(data: np.ndarray, window: int = 10):
    """检测最近window步是否单调，返回 (bool, slope)"""
    n = len(data)
    w = min(window, n)
    if w < 2:
        return False, 0.0
    diffs = np.diff(data[-w:])
    if np.all(diffs >= 0):
        return True, float(np.mean(diffs))
    if np.all(diffs <= 0):
        return True, float(np.mean(diffs))
    return False, 0.0


# ======================== 核心预测策略 ========================
def forecast_pattern_replay(data: np.ndarray, period: int, steps: int) -> list:
    """
    【主策略，高波动数据】历史波动直接重播，可选叠加趋势

    两种模式自动切换：

    模式A（近零趋势，slope/std < 0.05）：
      直接重播最近 n 个历史值，不做去趋势。
      replay 起始位置 = max(0, n - steps)，即从最近 steps 步的历史数据
      开始接续，让预测与历史尾部自然衔接。
      → 预测形状与历史分布完全一致，无任何漂移。

    模式B（显著趋势，slope/std >= 0.05）：
      去趋势 → 以最近 n 个残差为循环模板 → 叠加未来趋势。
      → 保留波动特征的同时延续趋势方向。

    两种模式均以全量历史（n 个点）为循环模板，不依赖 period，
    从而避免 period 很小（如 3）时的机械重复问题。
    """
    n = len(data)
    slope, intercept = linear_trend(data)
    data_std = float(np.std(data)) + 1e-10

    # 斜率相对于数据波动是否显著
    trend_significant = abs(slope) / data_std >= 0.05

    # 从最近 steps 步之前的位置开始重播，使预测与历史尾部衔接
    start = max(0, n - steps)

    if not trend_significant:
        # 模式A：直接重播原始值
        forecast = []
        for i in range(steps):
            idx = (start + i) % n
            forecast.append(float(data[idx]))
        return forecast
    else:
        # 模式B：去趋势后重播残差，叠加未来趋势
        trend_vals = slope * np.arange(n) + intercept
        residuals = data - trend_vals
        last_trend = slope * (n - 1) + intercept
        forecast = []
        for i in range(steps):
            idx = (start + i) % n
            future_trend = last_trend + slope * (i + 1)
            forecast.append(future_trend + float(residuals[idx]))
        return forecast


def forecast_holt_winters(data: np.ndarray, period: int, steps: int) -> list:
    """【低波动数据】Holt-Winters指数平滑"""
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    n = len(data)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            if n >= period * 2 and period >= 2:
                use_mul = np.min(data) > 1.0
                model = ExponentialSmoothing(
                    data,
                    trend="add",
                    damped_trend=True,
                    seasonal="mul" if use_mul else "add",
                    seasonal_periods=period,
                ).fit(optimized=True, maxiter=1000, disp=False)
            else:
                model = ExponentialSmoothing(
                    data,
                    trend="add",
                    damped_trend=True,
                ).fit(optimized=True, maxiter=1000, disp=False)
            return model.forecast(steps).tolist()
    except Exception:
        return []


def forecast_damped_linear(data: np.ndarray, slope: float, steps: int) -> list:
    """【单调数据】带阻尼的线性外推"""
    last = float(data[-1])
    # 阻尼因子：越往后，斜率衰减越多（避免无限外推）
    damp = 0.95
    forecast = []
    current_slope = slope
    val = last
    for _ in range(steps):
        val = val + current_slope
        current_slope *= damp
        forecast.append(val)
    return forecast


# ======================== 后处理 ========================
def clip_forecast(forecast: list, data: np.ndarray) -> list:
    """
    软边界裁剪：允许预测值在 [min - 1σ, max + 1σ] 之间。
    如果历史数据全非负，则额外保证预测值 >= 0。
    """
    data_min = float(np.min(data))
    data_max = float(np.max(data))
    data_std = float(np.std(data))
    all_non_negative = bool(np.all(data >= 0))

    lo = data_min - data_std
    hi = data_max + data_std
    if all_non_negative:
        lo = max(0.0, lo)

    result = []
    for v in forecast:
        if not np.isfinite(v):
            v = float(np.mean(data[-10:]))
        result.append(float(np.clip(v, lo, hi)))
    return result


# ======================== 主入口 ========================
def main(data_str: str, forecast_length: int = 20, seasonal_periods: int = None) -> dict:
    """
    完整预测逻辑

    参数:
        data_str         : 逗号分隔的历史数据字符串（也接受 {"result": "..."} 格式）
        forecast_length  : 预测步数，默认20
        seasonal_periods : 季节性周期；None则自动识别

    返回:
        {"result": "预测值1,预测值2,..."}
    """
    default = {"result": ",".join(["0.00"] * forecast_length)}

    try:
        # 1. 解析输入
        if isinstance(data_str, dict):
            data_str = data_str.get('result', '')

        parts = [p.strip() for p in str(data_str).split(',') if p.strip()]
        if not parts:
            return default

        data = []
        prev = 0.0
        for p in parts:
            try:
                v = float(p)
                data.append(v if np.isfinite(v) else prev)
                prev = data[-1]
            except (ValueError, OverflowError):
                data.append(prev)

        n = len(data)
        if n == 0:
            return default
        if n == 1:
            return {"result": ",".join([f"{data[0]:.2f}"] * forecast_length)}

        data = np.array(data, dtype=float)

        # 2. 自动识别周期
        if seasonal_periods is None or seasonal_periods < 2 or seasonal_periods > n // 2:
            period = detect_period(data)
        else:
            period = int(seasonal_periods)
        period = max(2, min(period, n // 2, 60))

        # 3. 计算波动率（决定策略）
        recent = data[-min(30, n):]
        recent_mean_abs = float(np.mean(np.abs(recent))) + 1e-10
        volatility_ratio = float(np.std(recent)) / recent_mean_abs

        # 4. 选择预测策略
        forecast: list = []

        # 策略A：高波动（波动率 > 0.3）→ 去趋势 + 残差周期复现
        # 优先于单调检测，防止窗口内碰巧单调而丢失波动特征
        if volatility_ratio > 0.3:
            forecast = forecast_pattern_replay(data, period, forecast_length)

        # 策略B：低波动且近期严格单调 → 阻尼线性外推
        # 额外要求：slope 相对于整体 std 不能太小（排除常数序列的微小数值误差）
        if not forecast:
            mono, slope_val = is_monotonic(data, window=min(10, n))
            data_std = float(np.std(data))
            if mono and abs(slope_val) > max(1e-12, data_std * 0.01):
                forecast = forecast_damped_linear(data, slope_val, forecast_length)

        # 策略C：低波动 → Holt-Winters
        if not forecast:
            forecast = forecast_holt_winters(data, period, forecast_length)

        # 策略D：兜底 → 直接重复最近一个周期的值
        if not forecast:
            pat = data[-period:].tolist()
            forecast = [pat[i % len(pat)] for i in range(forecast_length)]

        # 5. 软边界裁剪
        forecast = clip_forecast(forecast, data)

        # 6. 格式化输出
        return {"result": ",".join(f"{v:.2f}" for v in forecast)}

    except Exception as e:
        print(f"预测失败: {e}")
        return default


# ======================== Dify 运行入口 ========================
try:
    if 'input_data' not in locals():
        input_data = {
            "result": (
                "660073.0,0.0,2999873.0,0.0,0.0,15.0,0.0,3479835.0,0.0,4289956.0,"
                "0.0,3899996.0,0.0,0.0,6000000.0,-6.679515999735547,0.0,0.0,4135562.0,3839817.0,"
                "0.0,155993.0,3494489.0,5397209.0,41.803071996579284,-1434.4137460863233,1499369.0,"
                "0.0,-86.15725402200451,0.0,1800002.0,-234.69168573553515,0.0,-783.8555953909604,"
                "219.29141624820363,0.0,3599800.0,5445799.0,3599851.0,40.0,"
                "5.5979864043012455,60.0,28.319735058479633,-5999.999865889549,1088973.0,3601054.0,"
                "0.0,0.0,3928480.0,-192.76272445672208,839962.0,2399793.0,4679448.0,0.0,0.0,"
                "719972.0,5999999.0,479980.0,0.0,3599851.0,4289960.0,719970.0,1499369.0,0.0,15.0,"
                "5822358.0,0.0,1799998.0,0.0,0.0,5091506.0,1799.201303309563,2999869.0,839962.0,"
                "0.0,219.29141624820363,5999999.0,4673836.0,1798.566047639424,5849946.0,0.0,"
                "-192.76272445672208,-1420.1030375318674,4289960.0,0.0,40.0,3599742.0,3839817.0,"
                "3479835.0,0.0,1088966.0,-1434.4393032264502,155994.0,-192.77889171507792,3979980.0,"
                "-169.0771832141163,4184195.0,2399901.0,0.0,660073.0,0.0,0.0,0.0,"
                "-6.679515999735547,479980.0,0.0,3899996.0,0.0,60.0,17.967328674388188,0.0,0.0,"
                "0.0,0.0,0.0,5999755.0,0.0,-38.697657976851964,1079240.0,0.0,0.0,4184118.0,"
                "-1421.0813461204566,0.0,0.0,155994.0,3979777.0,-1434.3714513359735,959958.0,"
                "3479690.0,0.0,3601053.0,0.0,3899985.0,3599742.0,-169.0771832141163,2399897.0,0.0,"
                "1794.1867094729118,60.0,-192.76272445672208,-6.679515999735547,0.0,3839840.0,0.0,"
                "0.0,0.0,0.0,155989.0,0.0,1199949.0,5399779.0,4289960.0,-94.26249511420775,0.0,"
                "1199948.0,5999998.0,-7.787539160514075,0.0,5768389.0,719966.0,219.29141624820363,"
                "0.0,0.0,40.0,3599851.0,4016814.0,0.0,4679467.0,0.0,2999607.0,15.0,3301660.0,"
                "1370167.0,2955043.0,0.0,-12.06306352271615,0.0,3599516.0,5399776.0,"
                "219.29141624820363,0.0,0.0,0.0,3300000.0,15.0,10.154986887504936,0.0,3599670.0,"
                "-16.341635762105216,1088965.0,155989.0,0.0,0.0,-1421.89560700194,0.0,0.0,155993.0,"
                "5849946.0,2999607.0"
            )
        }

    if 'forecast_length' not in locals():
        forecast_length = 20

    if 'seasonal_periods' not in locals():
        seasonal_periods = None

    data_str = input_data.get('result', '') if isinstance(input_data, dict) else str(input_data)
    result = main(data_str, forecast_length, seasonal_periods)
    result

except Exception as exc:
    {"result": f"预测失败: {str(exc)}", "error": True}
