#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LOF 基金溢价监控脚本
数据源：天天基金网 (净值) + 东方财富 (场内价格)
通知方式：Server酱 (微信推送)
"""

import os
import requests
import time
from datetime import datetime, timedelta

# ========== 配置区 ==========
# 监控的 LOF 基金列表，格式：{"代码": "基金名称"}
FUNDS = {
    "161125": "标普500",
    "161128": "标普信息技术",
    "164906": "中国互联网",
    "164824": "工银瑞信印度市场",
    "162415": "美国消费",
    "160140": "美国房地产",
    "163208": "全球医疗",
    "160323": "华夏港股通",
    "501018": "南方原油",
    "162411": "华宝油气",
}

# 溢价率阈值（绝对值），超过此值才推送（%）
THRESHOLD = 1.0

# Server酱 SendKey（从 https://sct.ftqq.com/ 获取）
SERVERCHAN_KEY = os.environ.get("SERVERCHAN_KEY", "")

# 是否推送折价（默认只推溢价）
PUSH_DISCOUNT = False
# ========== 配置区结束 ==========


def get_nav(fund_code):
    """获取基金净值（天天基金网）"""
    url = f"https://api.fund.eastmoney.com/f10/lsjz?fundCode={fund_code}&pageIndex=1&pageSize=1"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://fund.eastmoney.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("Data", {}).get("LSJZList"):
            item = data["Data"]["LSJZList"][0]
            return float(item["DWJZ"]), item["FSRQ"]
    except Exception as e:
        print(f"  获取净值失败: {e}")
    return None, None


def get_market_price(fund_code):
    """获取场内实时价格（东方财富）"""
    # 深圳 LOF 以 0. 开头，上海以 1. 开头
    # 161125 等深市 LOF 用 0.xxxxx，沪市用 1.xxxxx
    code_int = int(fund_code)
    if code_int >= 500000:
        secid = f"1.{fund_code}"  # 上海
    else:
        secid = f"0.{fund_code}"  # 深圳

    url = f"https://push2.eastmoney.com/api/qt/stock/get?secid={secid}&fields=f43,f44,f45,f46,f47,f48,f49,f50,f51,f52,f53,f54,f55,f56,f57,f58,f60,f107,f152,f168,f169,f170,f171"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://quote.eastmoney.com/",
    }
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("data"):
            price = data["data"].get("f43", 0) / 100.0  # 最新价，单位：分
            if price > 0:
                return price
    except Exception as e:
        print(f"  获取场内价格失败: {e}")
    return None


def calc_premium(nav, market_price):
    """计算溢价率"""
    if nav is None or market_price is None or nav == 0:
        return None
    return (market_price - nav) / nav * 100.0


def send_serverchan(title, content):
    """通过 Server酱 推送消息到微信"""
    if not SERVERCHAN_KEY:
        print("  [WARN] 未配置 SERVERCHAN_KEY，跳过推送")
        return False
    url = f"https://sctapi.ftqq.com/{SERVERCHAN_KEY}.send"
    data = {
        "title": title,
        "desp": content,
    }
    try:
        resp = requests.post(url, data=data, timeout=10)
        result = resp.json()
        if result.get("code") == 0:
            print("  ✓ 推送成功")
            return True
        else:
            print(f"  ✗ 推送失败: {result}")
    except Exception as e:
        print(f"  ✗ 推送异常: {e}")
    return False


def load_history():
    """加载历史溢价率"""
    history = {}
    try:
        with open("history.csv", "r", encoding="utf-8") as f:
            for line in f.readlines()[1:]:  # 跳过表头
                parts = line.strip().split(",")
                if len(parts) >= 4:
                    history[parts[0]] = float(parts[3])
    except FileNotFoundError:
        pass
    return history


def save_history(results):
    """保存本次结果到历史文件"""
    today = datetime.now().strftime("%Y-%m-%d")
    # 追加模式
    file_exists = os.path.exists("history.csv")
    with open("history.csv", "a", encoding="utf-8") as f:
        if not file_exists:
            f.write("date,code,name,premium\n")
        for code, name, premium in results:
            f.write(f"{today},{code},{name},{premium:.2f}\n")


def main():
    print(f"{'='*50}")
    print(f"LOF 溢价监控 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*50}")

    if not SERVERCHAN_KEY:
        print("\n[警告] 未设置 SERVERCHAN_KEY 环境变量，不会推送微信消息")
        print("[提示] 请在 GitHub Secrets 中设置 SERVERCHAN_KEY\n")

    history = load_history()
    results = []
    alerts = []

    for code, name in FUNDS.items():
        print(f"\n▶ 检查 {code} {name}")
        nav, nav_date = get_nav(code)
        if nav is None:
            print(f"  ✗ 无法获取净值")
            continue
        print(f"  净值: {nav:.4f} (日期: {nav_date})")

        market_price = get_market_price(code)
        if market_price is None:
            print(f"  ✗ 无法获取场内价格（可能未开盘）")
            continue
        print(f"  场内价: {market_price:.4f}")

        premium = calc_premium(nav, market_price)
        if premium is None:
            print(f"  ✗ 溢价率计算失败")
            continue

        status = "溢价" if premium > 0 else "折价"
        emoji = "📈" if premium > 0 else "📉"
        print(f"  {emoji} 溢价率: {premium:+.2f}% ({status})")

        results.append((code, name, premium))

        # 判断是否推送
        should_alert = False
        reason = ""
        if premium > THRESHOLD:
            should_alert = True
            reason = f"溢价率 {premium:+.2f}% 超过阈值 {THRESHOLD}%"
        elif PUSH_DISCOUNT and premium < -THRESHOLD:
            should_alert = True
            reason = f"折价率 {premium:+.2f}% 超过阈值 {THRESHOLD}%"

        if should_alert:
            alerts.append((code, name, premium, nav, market_price, reason))

        # 溢价率方向变化提醒
        if code in history:
            old_premium = history[code]
            if (old_premium < 0 and premium > 0) or (old_premium > 0 and premium < 0):
                print(f"  ⚡ 溢价方向变化: {old_premium:+.2f}% → {premium:+.2f}%")

        time.sleep(0.5)  # 避免请求过快

    # 保存历史
    save_history(results)
    print(f"\n✓ 已保存 {len(results)} 条记录到 history.csv")

    # 推送提醒
    if alerts:
        print(f"\n{'='*50}")
        print(f"发现 {len(alerts)} 只基金超过阈值，准备推送...")
        print(f"{'='*50}")

        content_lines = ["## LOF 基金溢价提醒\n"]
        content_lines.append(f"**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        content_lines.append("| 代码 | 名称 | 净值 | 场内价 | 溢价率 |")
        content_lines.append("|------|------|------|--------|--------|")

        for code, name, premium, nav, market_price, reason in alerts:
            emoji = "🔴" if premium > 0 else "🟢"
            content_lines.append(
                f"| {code} | {name} | {nav:.4f} | {market_price:.4f} | {emoji} {premium:+.2f}% |"
            )

        content_lines.append(f"\n> 阈值设定: ±{THRESHOLD}%")
        content = "\n".join(content_lines)

        title = f"LOF溢价提醒：{len(alerts)}只基金超过阈值"
        send_serverchan(title, content)
    else:
        print("\n✓ 所有基金溢价率均在阈值范围内，无需推送")

    print(f"\n{'='*50}")
    print("完成")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()
