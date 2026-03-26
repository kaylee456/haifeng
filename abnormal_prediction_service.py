#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
异常数据预测服务
数据源：f_iot_unify_arr_detail 表，使用 array_join + regexp 方式提取参数值
支持 Power-1/2/3、Current-1/2/3、MachineStatus 参数查询，并自动标注异常时段
"""

import json
import logging
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Dict, Any, Tuple, Set

import pymysql
from flask import Flask, request, jsonify, Response

# ======================== 全局配置 ========================
@dataclass
class Config:
    # 数据库配置（来自代码1）
    DB_HOST: str = '10.20.200.26'
    DB_PORT: int = 9030
    DB_USER: str = 'root'
    DB_PASSWORD: str = 'hq-yp@inspur0925'
    DB_DATABASE: str = 'yp_bd'
    DB_CHARSET: str = 'utf8mb4'

    # 服务配置
    MAX_OUTPUT_POINTS: int = 200
    PORT: int = 4001
    HOST: str = '0.0.0.0'
    DEBUG: bool = False
    THREADED: bool = True

    # 业务配置：支持的参数名称
    ALLOWED_PARAMETERS: Tuple[str, ...] = (
        'Power-1', 'Power-2', 'Power-3',
        'Current-1', 'Current-2', 'Current-3',
        'MachineStatus'
    )

    # 异常状态码（MachineStatus 取这些值时视为异常）
    ERROR_STATUSES: Tuple[str, ...] = ('3',)

    # 异常时段前后扩展时间（分钟），标注用
    EXTEND_MINUTES: int = 10

    # 默认查询时间范围（分钟）
    DEFAULT_QUERY_MINUTES: int = 200
    # 最长允许查询范围（分钟），防止超大查询
    MAX_QUERY_MINUTES: int = 1440

    LOG_FILE: str = '/root/abnormal_prediction_service.log'

    # 设备编号验证正则（严格限制字符，防止注入）
    DEVICE_NO_PATTERN: re.Pattern = re.compile(r'^[a-zA-Z0-9_\-]{1,64}$')


# ======================== 日志配置 ========================
def setup_logging() -> logging.Logger:
    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    if logger.handlers:
        return logger

    formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    try:
        file_handler = logging.FileHandler(Config.LOG_FILE, encoding='utf-8')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception:
        pass  # 日志文件写不了时降级为只输出到控制台

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    return logger


# 初始化全局对象
app = Flask(__name__)
config = Config()
logger = setup_logging()


# ======================== 数据库操作 ========================
def get_db_connection() -> pymysql.connections.Connection:
    """获取数据库连接"""
    try:
        conn = pymysql.connect(
            host=config.DB_HOST,
            port=config.DB_PORT,
            user=config.DB_USER,
            password=config.DB_PASSWORD,
            database=config.DB_DATABASE,
            charset=config.DB_CHARSET,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=10,
            read_timeout=60,
        )
        return conn
    except pymysql.MySQLError as e:
        logger.error(f"数据库连接失败: {e}", exc_info=True)
        raise


# ======================== 参数验证 ========================
def validate_request(device_no: str, parameter: str) -> None:
    """
    验证请求参数合法性

    Raises:
        ValueError: 参数不合法时抛出
    """
    if not device_no or not config.DEVICE_NO_PATTERN.match(device_no):
        raise ValueError(
            "device_no 不合法：只能包含字母、数字、下划线、连字符，长度1-64位"
        )
    if parameter not in config.ALLOWED_PARAMETERS:
        raise ValueError(
            f"parameter 不合法：仅支持 {list(config.ALLOWED_PARAMETERS)}，当前值：{parameter}"
        )


def parse_minutes(raw: str, default: int) -> int:
    """解析 minutes 参数，超出范围时截断"""
    try:
        val = int(raw)
        if val <= 0:
            return default
        return min(val, config.MAX_QUERY_MINUTES)
    except (TypeError, ValueError):
        return default


# ======================== 核心查询逻辑（来自代码1） ========================
def fetch_raw_rows(device_no: str, minutes: int) -> List[Dict]:
    """
    查询指定设备最近 N 分钟的原始数据行
    使用 array_join(d_array, ',') 一次性拉取所有字段
    """
    sql = f"""
    SELECT
        datatime,
        device_no,
        array_join(d_array, ',') AS raw_array
    FROM f_iot_unify_arr_detail
    WHERE datatime >= DATE_SUB(NOW(), INTERVAL {int(minutes)} MINUTE)
      AND datatime <= NOW()
      AND device_no = '{device_no}'
    ORDER BY datatime ASC
    """
    logger.info(f"查询原始数据: device_no={device_no}, 最近{minutes}分钟")
    logger.debug(f"SQL: {sql.strip()}")

    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(sql)
            rows = cursor.fetchall()
        logger.info(f"原始行数: {len(rows)}")
        return rows
    finally:
        conn.close()


def extract_param_value(raw_array: str, param: str) -> Any:
    """
    从 array_join 拼接的字符串中提取指定参数的值（来自代码1的 regex 逻辑）
    格式示例：...,"key":"Power-1","value":"12.34",...
    """
    if not raw_array:
        return None
    pattern = f'"key":"{re.escape(param)}","value":"([^"]*)"'
    match = re.search(pattern, raw_array)
    return match.group(1) if match else None


def parse_rows(rows: List[Dict], parameter: str) -> List[Dict]:
    """
    解析原始行，提取目标参数值和 MachineStatus，并标注异常时刻

    异常时刻定义：MachineStatus 在 ERROR_STATUSES 内时，该行及其前后
    EXTEND_MINUTES 分钟内的行均被标注为异常时段（is_error_period=1）。
    """
    if not rows:
        return []

    # 第一遍：提取每行的参数值和 MachineStatus
    parsed: List[Dict] = []
    for row in rows:
        raw = row.get('raw_array', '') or ''
        dt = row['datatime']
        if isinstance(dt, datetime):
            dt_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            dt_obj = dt
        else:
            dt_str = str(dt)
            dt_obj = datetime.strptime(dt_str, '%Y-%m-%d %H:%M:%S')

        status = extract_param_value(raw, 'MachineStatus')

        item: Dict[str, Any] = {
            'datatime': dt_str,
            '_dt_obj': dt_obj,
            'device_no': row['device_no'],
            'MachineStatus': status,
        }

        # 若查询的就是 MachineStatus，不需要再单独提取
        if parameter != 'MachineStatus':
            val = extract_param_value(raw, parameter)
            # 尝试转数值，保留原始字符串作为备用
            if val is not None:
                try:
                    item[parameter] = float(val)
                except (ValueError, TypeError):
                    item[parameter] = val
            else:
                item[parameter] = None
        else:
            # 直接使用 MachineStatus 的值
            item[parameter] = status

        parsed.append(item)

    # 第二遍：确定异常时刻集合，扩展前后 EXTEND_MINUTES
    error_moments: Set[str] = set()
    for item in parsed:
        if str(item.get('MachineStatus', '')).strip() in config.ERROR_STATUSES:
            dt_obj = item['_dt_obj']
            for delta in range(-config.EXTEND_MINUTES * 60, config.EXTEND_MINUTES * 60 + 1, 1):
                # 按秒扩展
                t = dt_obj + timedelta(seconds=delta)
                error_moments.add(t.strftime('%Y-%m-%d %H:%M:%S'))

    # 第三遍：标注 is_error_period，清理内部字段
    result: List[Dict[str, Any]] = []
    for item in parsed:
        is_err = 1 if item['datatime'] in error_moments else 0
        out = {
            'datatime': item['datatime'],
            'device_no': item['device_no'],
            parameter: item[parameter],
            'MachineStatus': item['MachineStatus'],
            'is_error_period': is_err,
        }
        result.append(out)

    error_cnt = sum(1 for r in result if r['is_error_period'] == 1)
    logger.info(f"解析完成：共{len(result)}行，其中异常时段{error_cnt}行")
    return result


# ======================== 下采样 ========================
def downsample_data(data: List[Dict]) -> List[Dict]:
    """等间隔下采样，最多保留 MAX_OUTPUT_POINTS 个点"""
    n = len(data)
    if n <= config.MAX_OUTPUT_POINTS:
        return data

    step = n / config.MAX_OUTPUT_POINTS
    sampled = [data[int(round(i * step))] for i in range(config.MAX_OUTPUT_POINTS)
               if int(round(i * step)) < n]
    logger.info(f"下采样: {n} → {len(sampled)} 条")
    return sampled


# ======================== 输出格式化 ========================
def format_output(data: List[Dict]) -> str:
    """每行一个 JSON 对象，与代码2保持一致"""
    lines = []
    for item in data:
        try:
            lines.append(json.dumps(item, ensure_ascii=False, default=str))
        except Exception as e:
            logger.warning(f"序列化失败，跳过: {e} | {item}")
    return '\n'.join(lines)


# ======================== 接口定义 ========================
@app.route('/health', methods=['GET'])
def health_check() -> Response:
    """健康检查"""
    db_status = 'disconnected'
    try:
        conn = get_db_connection()
        conn.close()
        db_status = 'connected'
    except Exception:
        pass

    return jsonify({
        'status': 'ok',
        'service': 'abnormal_prediction_service',
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'database': db_status,
        'allowed_parameters': list(config.ALLOWED_PARAMETERS),
        'error_statuses': list(config.ERROR_STATUSES),
        'extend_minutes': config.EXTEND_MINUTES,
    })


@app.route('/query', methods=['GET', 'POST'])
def data_query():
    """
    数据查询接口（兼容 GET / POST）

    请求参数：
        device_no  (必填) 设备编号，如 YPFCL06XXXXXM01
        parameter  (必填) 参数名，支持 Power-1/2/3, Current-1/2/3, MachineStatus
        minutes    (选填) 查询最近 N 分钟，默认200，最大1440

    返回：
        {
          "text": "<每行一个 JSON 对象的字符串>",
          "meta": {
            "total_raw": <原始行数>,
            "total_sampled": <下采样后行数>,
            "parameter": ...,
            "device_no": ...,
            "minutes": ...,
            "error_statuses": [...],
            "extend_minutes": ...
          }
        }
    """
    try:
        # 解析请求参数
        if request.method == 'POST':
            req_data = request.get_json(silent=True) or request.form.to_dict()
        else:
            req_data = request.args.to_dict()

        device_no = (req_data.get('device_no') or '').strip()
        parameter = (req_data.get('parameter') or '').strip()
        minutes = parse_minutes(req_data.get('minutes'), config.DEFAULT_QUERY_MINUTES)

        # 校验必填参数
        if not parameter:
            return jsonify({
                'error': '缺少参数',
                'message': f'必须提供 parameter，支持：{list(config.ALLOWED_PARAMETERS)}',
            }), 400

        if not device_no:
            return jsonify({
                'error': '缺少参数',
                'message': '必须提供 device_no（设备编号）',
            }), 400

        # 严格验证
        validate_request(device_no, parameter)

        # 查询原始数据
        raw_rows = fetch_raw_rows(device_no, minutes)

        # 解析并标注异常时段
        parsed_data = parse_rows(raw_rows, parameter)

        # 下采样
        sampled_data = downsample_data(parsed_data)

        # 格式化输出
        output_text = format_output(sampled_data)

        return jsonify({
            'text': output_text,
            'meta': {
                'total_raw': len(parsed_data),
                'total_sampled': len(sampled_data),
                'parameter': parameter,
                'device_no': device_no,
                'minutes': minutes,
                'error_statuses': list(config.ERROR_STATUSES),
                'extend_minutes': config.EXTEND_MINUTES,
            },
        }), 200

    except ValueError as e:
        return jsonify({'error': '参数错误', 'message': str(e)}), 400
    except Exception as e:
        logger.error(f"接口处理异常: {e}", exc_info=True)
        return jsonify({'error': '服务器内部错误', 'message': '服务暂时不可用，请稍后重试'}), 500


@app.route('/query_error_periods', methods=['GET', 'POST'])
def query_error_periods():
    """
    查询指定设备最近 N 分钟内的异常时段（MachineStatus 在 ERROR_STATUSES 内的时间点）

    请求参数：
        device_no  (必填) 设备编号
        minutes    (选填) 查询最近 N 分钟，默认200，最大1440

    返回：
        {
          "device_no": ...,
          "error_statuses": [...],
          "total_error_points": <异常时间点数>,
          "error_points": [{"datatime": ..., "MachineStatus": ...}, ...]
        }
    """
    try:
        if request.method == 'POST':
            req_data = request.get_json(silent=True) or request.form.to_dict()
        else:
            req_data = request.args.to_dict()

        device_no = (req_data.get('device_no') or '').strip()
        minutes = parse_minutes(req_data.get('minutes'), config.DEFAULT_QUERY_MINUTES)

        if not device_no:
            return jsonify({'error': '缺少参数', 'message': '必须提供 device_no'}), 400

        if not config.DEVICE_NO_PATTERN.match(device_no):
            return jsonify({'error': '参数错误', 'message': 'device_no 格式不合法'}), 400

        status_str = ','.join(f"'{s}'" for s in config.ERROR_STATUSES)
        sql = f"""
        SELECT DISTINCT
            datatime,
            device_no
        FROM f_iot_unify_arr_detail
        WHERE datatime >= DATE_SUB(NOW(), INTERVAL {int(minutes)} MINUTE)
          AND datatime <= NOW()
          AND device_no = '{device_no}'
          AND (
              regexp_extract(array_join(d_array, ','), '"key":"MachineStatus","value":"([^"]*)"', 1)
              IN ({status_str})
          )
        ORDER BY datatime ASC
        """

        logger.info(f"查询异常时间点: device_no={device_no}, 最近{minutes}分钟")
        conn = get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                rows = cursor.fetchall()
        finally:
            conn.close()

        error_points = []
        for row in rows:
            dt = row['datatime']
            dt_str = dt.strftime('%Y-%m-%d %H:%M:%S') if isinstance(dt, datetime) else str(dt)
            error_points.append({'datatime': dt_str, 'device_no': row['device_no']})

        logger.info(f"找到 {len(error_points)} 个异常时间点")
        return jsonify({
            'device_no': device_no,
            'error_statuses': list(config.ERROR_STATUSES),
            'minutes': minutes,
            'total_error_points': len(error_points),
            'error_points': error_points,
        }), 200

    except Exception as e:
        logger.error(f"异常时段查询失败: {e}", exc_info=True)
        return jsonify({'error': '服务器内部错误', 'message': str(e)}), 500


# ======================== 启动服务 ========================
if __name__ == '__main__':
    logger.info("=" * 50)
    logger.info("启动异常数据预测服务")
    logger.info(f"服务地址：http://{config.HOST}:{config.PORT}")
    logger.info(f"允许参数：{config.ALLOWED_PARAMETERS}")
    logger.info(f"异常状态：{config.ERROR_STATUSES}")
    logger.info(f"异常扩展：±{config.EXTEND_MINUTES} 分钟")
    logger.info(f"最大返回点数：{config.MAX_OUTPUT_POINTS}")
    logger.info(f"数据库地址：{config.DB_HOST}:{config.DB_PORT}/{config.DB_DATABASE}")
    logger.info("=" * 50)

    app.run(
        host=config.HOST,
        port=config.PORT,
        debug=config.DEBUG,
        threaded=config.THREADED,
        use_reloader=False,
    )
