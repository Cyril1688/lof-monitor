#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LOF 基金溢价监控脚本 v3.0

修复说明:
v2.0 问题: 使用"实际净值"(上一交易日)，导致与集思录差异很大
v3.0 修复: 改用"实时估算净值"，与集思录保持一致

数据源:
  - 场内价格: 新浪行情API (hq.sinajs.cn) [主] / 东方财富K线 [备]
  - 净值/估值: 东方财富估算净值API (fundgz.1234567.com.cn)
  - 交叉验证: 集思录API (jisilu.cn)

通知方式: Server酱 (微信推送)
"""

import os
import re
import json
import requests
import time
from datetime import datetime, timedelta

# ========== 配置区 ==========
THRESHOLD = 2.0          # 溢价率阈值(%)
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")
PUSH_DISCOUNT = False    # 是否推送折价
REQUEST_DELAY = 0.12     # 请求间隔(秒)
# ========== 配置区结束 ==========
def create_session():
    """创建不使用系统代理的session"""
    s = requests.Session()
    s.trust_env = False
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    return s, headers


def get_fund_data(fund_code, session, headers):
    """
    获取基金完整数据: 场内价 + 估算净值 + 上日净值
    
    返回:
        {
            'price': float or None,      # 场内现价
            'prev_close': float or None,  # 昨收
            'estimate_nav': float or None,# 估算净值
            'actual_nav': float or None,  # 上日实际净值
            'nav_date': str or None,      # 净值日期  
            'nav_change': float or None,  # 估算增长率%
            'name': str or None,
            'source': str,                # 数据来源标注
        }
    """
    result = {
        'price': None, 'prev_close': None,
        'estimate_nav': None, 'actual_nav': None,
        'nav_date': None, 'nav_change': None,
        'name': None, 'source': ''
    }

    # === 1. 获取估值数据 (东方财富 fundgz) ===
    try:
        url = f"https://fundgz.1234567.com.cn/js/{fund_code}.js"
        h = {**headers, "Referer": "https://fund.eastmoney.com/"}
        resp = session.get(url, headers=h, timeout=8)
        if resp.status_code == 200 and "gsz" in resp.text:
            text = resp.text
            start = text.index("{")
            end = text.rindex("}") + 1
            data = json.loads(text[start:end])
            
            result['actual_nav'] = float(data.get("dwjz", 0))
            result['estimate_nav'] = float(data.get("gsz", 0))
            result['nav_date'] = data.get("jzrq", "")
            result['nav_change'] = float(data.get("gszzl", 0))
            result['name'] = data.get("name", "")
            result['source'] += "[估值OK]"
    except Exception as e:
        result['source'] += f"[估值ERR:{type(e).__name__}]"

    # === 2. 获取场内价格 (新浪 hq.sinajs.cn) ===
    code_int = int(fund_code)
    if code_int >= 500000:
        symbol = f"sh{fund_code}"
    else:
        symbol = f"sz{fund_code}"

    try:
        url = f"https://hq.sinajs.cn/list={symbol}"
        h = {**headers, "Referer": "https://finance.sina.com.cn/"}
        resp = session.get(url, headers=h, timeout=8)
        if resp.status_code == 200 and '"' in resp.text:
            parts = resp.text.split('"')[1].split(',')
            if len(parts) >= 32 and parts[3]:
                result['price'] = float(parts[3])       # 现价
                result['prev_close'] = float(parts[2])   # 昨收
                result['source'] += "[新浪]"
                
                if not result['name']:
                    result['name'] = parts[0]
    except Exception as e:
        result['source'] += f"[新浪ERR:{type(e).__name__}]"

    # === 3. 如果新浪失败, 尝试东方财富K线 (push2his) ===
    if result['price'] is None:
        secid = f"1.{fund_code}" if code_int >= 500000 else f"0.{fund_code}"
        try:
            url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
                   f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
                   f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
                   f"&klt=101&fqt=0&end=20500101&lmt=1")
            h = {**headers, "Referer": "https://quote.eastmoney.com/"}
            resp = session.get(url, headers=h, timeout=8)
            if resp.status_code == 200:
                data = resp.json()
                klines = data.get("data", {}).get("klines", [])
                if klines:
                    parts = klines[0].split(",")
                    if len(parts) >= 3:
                        price_raw = float(parts[2])
                        # 判断单位: LOF基金价格通常在0.5~10之间
                        if price_raw > 100:
                            result['price'] = price_raw / 1000.0  # 厘→元
                        else:
                            result['price'] = price_raw
                        result['source'] += "[东财K线]"
        except Exception:
            pass

    return result
def calculate_premium(data):
    """
    计算溢价率
    
    优先使用估算净值(与集思录一致), fallback到实际净值
    """
    price = data['price']
    if not price:
        return None, None, "无场内价"

    # 优先用估算净值
    nav = data['estimate_nav']
    nav_type = "估算净值"
    
    # 如果估算净值不可用或异常, 用实际净值
    if not nav or nav <= 0:
        nav = data['actual_nav']
        nav_type = "实际净值"
    
    if not nav or nav <= 0:
        return None, None, "无净值"
    
    premium = (price - nav) / nav * 100.0
    
    # 异常保护: 溢价率超过50%可能是数据错误
    if abs(premium) > 50:
        return premium, nav_type, f"[!]异常高溢价{premium:+.1f}%,请人工核实"
    
    return premium, nav_type, ""
def get_jisilu_data(session, headers):
    """从集思录获取LOF数据用于交叉验证"""
    jsl = {}
    try:
        url = "https://www.jisilu.cn/data/lof/stock_lof_list/?___jsl=LST&rp=100"
        h = {
            **headers,
            "Referer": "https://www.jisilu.cn/data/lof/",
            "X-Requested-With": "XMLHttpRequest",
        }
        resp = session.get(url, headers=h, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            for row in data.get("rows", []):
                cell = row.get("cell", {})
                code = cell.get("fund_id", "")
                if code:
                    jsl[code] = {
                        "name": cell.get("fund_nm", ""),
                        "price": cell.get("price", ""),
                        "nav": cell.get("fund_nav", ""),
                        "premium": cell.get("nav_discount_rt", ""),
                    }
    except Exception as e:
        print(f"[集思录] 获取失败: {e}")
    return jsl
def send_serverchan(title, content):
    """通过Server酱推送消息到微信"""
    if not SERVERCHAN_KEY:
        print("  [WARN] 未配置 SERVERCHAN_KEY")
        return False
    url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
    try:
        resp = requests.post(url, data={"title": title, "desp": content}, timeout=10)
        return resp.json().get("code") == 0
    except Exception as e:
        print(f"  ✗ 推送异常: {e}")
    return False
def save_history(results):
    """保存本次结果,只保留最近7天"""
    today = datetime.now().strftime("%Y-%m-%d")
    existed = os.path.exists("history.csv")
    with open("history.csv", "a", encoding="utf-8") as f:
        if not existed:
            f.write("date,code,name,premium,nav_type\n")
        for code, name, premium, nav_type in results:
            f.write(f"{today},{code},{name},{premium:.2f},{nav_type}\n")

    try:
        cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
        with open("history.csv", "r", encoding="utf-8") as f:
            lines = f.readlines()
        kept = [lines[0]]
        for line in lines[1:]:
            if line.strip().split(",")[0] >= cutoff:
                kept.append(line)
        with open("history.csv", "w", encoding="utf-8") as f:
            f.writelines(kept)
        removed = len(lines) - len(kept)
        if removed > 0:
            print(f"  清理历史: {removed} 条旧记录")
    except Exception:
        pass
def main():
    print(f"{'='*60}")
    print(f"LOF 溢价监控 v3.0 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}")
    print("[改进] 使用实时估算净值计算溢价(与集思录一致)")
    print()

    # 加载基金列表
    funds = {}
    funds_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "funds.txt")
    try:
        with open(funds_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split(None, 1)
                if len(parts) >= 2:
                    funds[parts[0].strip()] = parts[1].strip()
    except FileNotFoundError:
        print(f"[错误] 找不到 {funds_file}")
        return

    print(f"[基金] 共 {len(funds)} 只LOF | [阈值] ±{THRESHOLD}%")
    print()

    # 创建网络session
    session, headers = create_session()

    # 获取集思录数据用于交叉验证
    jsl_data = get_jisilu_data(session, headers)
    if jsl_data:
        print(f"[集思录] 获取 {len(jsl_data)} 只基金用于验证\n")

    alerts = []
    results = []
    verified = 0
    mismatched = 0
    no_data = 0
    total = 0

    for code, name in funds.items():
        total += 1
        print(f"({total}/{len(funds)}) {code} {name}", end=" ")

        # 获取数据
        data = get_fund_data(code, session, headers)

        if not data['price']:
            print("✗ 无场内价 " + data['source'])
            no_data += 1
            time.sleep(REQUEST_DELAY)
            continue

        # 计算溢价率
        premium, nav_type, warning = calculate_premium(data)

        if premium is None:
            print(f"✗ 无法计算 " + data['source'])
            no_data += 1
            time.sleep(REQUEST_DELAY)
            continue

        # 输出信息
        nav_display = data['estimate_nav'] if data['estimate_nav'] else data['actual_nav']
        info = f"现价{data['price']:.3f} {nav_type}{nav_display:.4f} → {premium:+.2f}%"
        
        # 对比集思录
        if code in jsl_data:
            jsl = jsl_data[code]
            try:
                jsl_prem = float(jsl["premium"]) if jsl["premium"] != "-" else None
            except (ValueError, TypeError):
                jsl_prem = None
            
            if jsl_prem is not None:
                diff = abs(premium - jsl_prem)
                if diff < 1.0:
                    verified += 1
                    info += f" ✓集思录{jsl_prem:+.1f}%"
                else:
                    mismatched += 1
                    info += f" △集思录{jsl_prem:+.1f}%"

        if warning:
            info += f" {warning}"

        print(info)

        # 记录
        results.append((code, name, premium, nav_type))

        # 判断是否超过阈值
        if premium > THRESHOLD:
            alerts.append({
                'code': code, 'name': name,
                'premium': premium, 'nav_type': nav_type,
                'price': data['price'], 'nav': nav_display,
                'warning': warning
            })
        elif PUSH_DISCOUNT and premium < -THRESHOLD:
            alerts.append({
                'code': code, 'name': name,
                'premium': premium, 'nav_type': nav_type,
                'price': data['price'], 'nav': nav_display,
                'warning': warning
            })

        time.sleep(REQUEST_DELAY)

    # 保存历史
    save_history(results)

    # 统计
    print(f"\n{'='*60}")
    print(f"[统计] 总数:{total} 有效:{len(results)} 无数据:{no_data} 超阈值:{len(alerts)}")
    if jsl_data:
        print(f"[交叉验证] 一致:{verified} 差异:{mismatched}")
    print(f"{'='*60}")

    # 推送
    if alerts:
        alerts.sort(key=lambda x: x['premium'], reverse=True)

        content_lines = [
            "## 📊 LOF 基金溢价提醒",
            "",
            f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"**阈值**: ±{THRESHOLD}%",
            f"**统计**: {len(alerts)} 只超过阈值",
            f"**数据源**: 估算净值(实时)",
            "",
            "| 代码 | 名称 | 场内价 | 净值(估) | 溢价率 |",
            "|------|------|--------|----------|--------|",
        ]

        for a in alerts:
            emoji = "🔴" if a['premium'] > 0 else "🟢"
            content_lines.append(
                f"| {a['code']} | {a['name']} | {a['price']:.3f} | {a['nav']:.4f} "
                f"| {emoji} **{a['premium']:+.2f}%** |"
            )

        content = "\n".join(content_lines)
        title = f"LOF溢价提醒：{len(alerts)}只超过{THRESHOLD}%"
        send_serverchan(title, content)
        print(f"\n[推送] 已发送 {len(alerts)} 条预警")
    else:
        print(f"\n✓ 所有基金均在 ±{THRESHOLD}% 范围内")

    print(f"\n{'='*60}")
if __name__ == "__main__":
    main()
