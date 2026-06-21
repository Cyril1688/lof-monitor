#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LOF 基金溢价监控脚本 v2.0
数据源: 东方财富 (场内价) + 天天基金 (净值) + 集思录 (交叉验证)
通知方式: Server酱 (微信推送)
"""

import os
import requests
import time
from datetime import datetime, timedelta

# ========== 配置区 ==========
# 溢价率阈值（绝对值），超过此值才推送（%）
THRESHOLD = 5.0

# Server酱 SendKey
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")

# 是否推送折价
PUSH_DISCOUNT = False

# 请求间隔（秒）
REQUEST_DELAY = 0.15
# ========== 配置区结束 ==========


def get_jisilu_data():
    """从集思录获取LOF数据用于交叉验证（仅20只热门）"""
    jsl = {}
    try:
        url = "https://www.jisilu.cn/data/lof/stock_lof_list/?___jsl=LST&rp=50"
        resp = requests.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
            "Referer": "https://www.jisilu.cn/data/lof/",
        }, timeout=10)
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
        print(f"[集思录] 获取失败: {e}（不影响主流程）")
    return jsl


def get_nav(fund_code):
    """获取基金最新净值（天天基金）"""
    url = f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={fund_code}&pageIndex=1&pageSize=1"
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://fund.eastmoney.com/"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("Data", {}).get("LSJZList"):
            item = data["Data"]["LSJZList"][0]
            return float(item["DWJZ"]), item["FSRQ"]
    except Exception:
        pass
    return None, None


def get_market_price(fund_code):
    """
    获取场内实时价格（东方财富）
    基金价格单位为 "厘"（÷1000），不是 "分"（÷100）
    例如: 1211 → 1.211, 7059 → 7.059
    """
    code_int = int(fund_code)
    secid = f"1.{fund_code}" if code_int >= 500000 else f"0.{fund_code}"

    url = (f"https://push2his.eastmoney.com/api/qt/stock/kline/get"
           f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
           f"&fields2=f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
           f"&klt=101&fqt=0&end=20500101&lmt=1")
    headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://quote.eastmoney.com/"}

    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        klines = data.get("data", {}).get("klines", [])
        if klines:
            parts = klines[0].split(",")
            if len(parts) >= 3:
                # f52: 收盘价 (close), 单位是厘 (÷1000 for funds)
                close_price = float(parts[2]) / 1000.0
                if close_price > 0:
                    return close_price
    except Exception:
        pass

    # 备用: 使用实时行情接口
    try:
        url2 = (f"https://push2.eastmoney.com/api/qt/stock/get"
                f"?secid={secid}&fields=f43")
        resp2 = requests.get(url2, headers=headers, timeout=10)
        data2 = resp2.json()
        raw_price = data2.get("data", {}).get("f43", 0)
        if raw_price > 0:
            price = raw_price / 1000.0  # 厘→元
            return price
    except Exception:
        pass

    return None


def send_serverchan(title, content):
    """通过 Server酱 推送消息到微信"""
    if not SERVERCHAN_KEY:
        print("  [WARN] 未配置 SERVERCHAN_KEY，跳过推送")
        return False
    url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
    try:
        resp = requests.post(url, data={"title": title, "desp": content}, timeout=10)
        if resp.json().get("code") == 0:
            return True
    except Exception as e:
        print(f"  ✗ 推送异常: {e}")
    return False


def save_history(results):
    """保存本次结果，只保留最近7天"""
    today = datetime.now().strftime("%Y-%m-%d")
    existed = os.path.exists("history.csv")
    with open("history.csv", "a", encoding="utf-8") as f:
        if not existed:
            f.write("date,code,name,premium\n")
        for code, name, premium in results:
            f.write(f"{today},{code},{name},{premium:.2f}\n")

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
    print(f"{'='*55}")
    print(f"LOF 溢价监控 v2.0 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*55}")

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

    # 获取集思录数据用于交叉验证
    jsl_data = get_jisilu_data()
    if jsl_data:
        print(f"[集思录] 获取 {len(jsl_data)} 只基金用于交叉验证\n")

    alerts = []
    results = []
    verified = 0
    mismatched = 0
    total = 0

    for code, name in funds.items():
        total += 1
        print(f"▶ ({total}/{len(funds)}) {code} {name}")

        # 获取净值
        nav, nav_date = get_nav(code)
        if nav is None:
            print(f"  ✗ 净值获取失败")
            continue
        print(f"  净值: {nav:.4f} ({nav_date})")

        # 获取场内价
        market_price = get_market_price(code)
        if market_price is None:
            print(f"  ✗ 场内价获取失败（可能未开盘或已停牌）")
            continue
        print(f"  场内价: {market_price:.4f}")

        # 计算溢价率
        premium = (market_price - nav) / nav * 100.0

        # 交叉验证：对比集思录数据
        if code in jsl_data:
            jsl = jsl_data[code]
            jsl_premium_str = jsl.get("premium", "-")
            try:
                jsl_premium = float(jsl_premium_str) if jsl_premium_str != "-" else None
            except (ValueError, TypeError):
                jsl_premium = None

            if jsl_premium is not None:
                diff = abs(premium - jsl_premium)
                if diff < 0.5:  # 差异小于0.5%认为一致
                    verified += 1
                    print(f"  ✓ 集思录核验一致 ({premium:+.2f}% ≈ {jsl_premium:+.2f}%)")
                else:
                    mismatched += 1
                    flag = "⚠️" if diff > 3 else ""
                    print(f"  {flag} 与集思录差异: 我们{premium:+.2f}%, 集思录{jsl_premium:+.2f}%")
        else:
            print(f"  溢价率: {premium:+.2f}%")

        # 记录结果
        results.append((code, name, premium))

        # 判断是否超过阈值
        if premium > THRESHOLD:
            alerts.append((code, name, premium, nav, market_price, nav_date))
        elif PUSH_DISCOUNT and premium < -THRESHOLD:
            alerts.append((code, name, premium, nav, market_price, nav_date))

        time.sleep(REQUEST_DELAY)

    # 保存历史
    save_history(results)

    # 统计
    print(f"\n{'='*55}")
    print(f"[统计] 总数: {total} | 有效: {len(results)} | 超阈值: {len(alerts)}")
    if jsl_data:
        print(f"[交叉验证] 一致: {verified} | 有差异: {mismatched}")
    print(f"{'='*55}")

    # 推送
    if alerts:
        alerts.sort(key=lambda x: x[2], reverse=True)

        content_lines = [
            "## 📊 LOF 基金溢价提醒",
            "",
            f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"**阈值**: ±{THRESHOLD}%",
            f"**统计**: {len(alerts)} 只超过阈值",
            "",
            "| 代码 | 名称 | 净值 | 场内价 | 溢价率 |",
            "|------|------|------|--------|--------|",
        ]

        for code, name, premium, nav, mp, nd in alerts:
            emoji = "🔴" if premium > 0 else "🟢"
            content_lines.append(
                f"| {code} | {name} | {nav:.4f} | {mp:.4f} | {emoji} **{premium:+.2f}%** |"
            )

        content = "\n".join(content_lines)
        title = f"LOF溢价提醒：{len(alerts)}只基金超过{THRESHOLD}%阈值"
        send_serverchan(title, content)
    else:
        print(f"✓ 所有基金溢价率均在 ±{THRESHOLD}% 范围内，无需推送")

    print(f"\n{'='*55}")
    print(f"完成")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
