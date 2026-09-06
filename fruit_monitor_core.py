#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
萧山定位 3km 水果店实时监控核心模块(可被launchd/手动反复调用,无状态)。

职责:
  1) scrape()      —— 连常驻Chrome(CDP 9222)→滚屏→抓 店[名称/距离/时效/评分/月售/SKU[]]
  2) diff(prev,new)—— 逐店逐SKU对比, 产出价格变动/上下架/店铺增删
  3) build_message —— 构造微信正文
  4) push          —— pushplus推送到微信
  5) main          —— 单次闭环: 抓→对比→存history→推

设计要点:
  - 对比基准 = shangou/history/fruit_baseline.json (滚动保留最近N次的SKU身份/价格)
  - SKU身份指纹 = (imgUrl规范化, desc)  → 可稳定识别同一SKU, 价格随抓取变化即可检测涨跌
  - 失败容错: Chrome连不上/验证码/错误墙→回退推"最近一次成功快照"并标明异常, 不静默
  - 位置 = 杭州萧山·花屿观澜里附近3km(lat30.195608,lng120.260958)
"""
import os, sys, json, time, re, datetime

HERE = os.path.dirname(os.path.abspath(__file__))
HIST = os.path.join(HERE, "history")
os.makedirs(HIST, exist_ok=True)

# ===== 对比基线: 每条链路一个独立文件 =====
# 背景(2026-09-06 修复): fruit_loop.py(30分钟常驻) 与本脚本(10分钟定时任务) 共用
#   fruit_baseline.json, 两边代码版本/抓取口径不一致时(loop 曾跑旧代码、不过 3km 过滤,
#   把 55 家全量写进基线), diff 会交替刷出「消失47 / 新店52」等几十条假变动, 淹没真实信号。
# 现在按链路分文件, 互不干扰:
#   core  = 10分钟自动化(默认)      -> history/fruit_baseline.json
#   guard = fruit_guard.sh          -> history/fruit_baseline_guard.json
#   loop  = fruit_loop.py           -> history/fruit_baseline_loop.json
# 覆盖方式: BASELINE_TAG=loop python fruit_monitor_core.py
_TAG = (os.environ.get("BASELINE_TAG") or "core").strip()
BASELINE = os.path.join(HIST, "fruit_baseline.json" if _TAG == "core" else f"fruit_baseline_{_TAG}.json")

# ===== 消失/下架 去抖(防止上游 ETA/分页抖动造成假报) =====
# h5.ele.me 的 etaMin 会随页面状态漂移(同一批店有时 15min、有时 22min), 导致某家店
# 这一轮进不了 3km 过滤、下一轮又回来。若"一次没抓到就报消失", 就会刷出几十条假警报。
# 现在: 连续 MISS_THRESHOLD 轮都没出现才判定消失/下架, 且只播报一次;
#       影子条目最多保留 KEEP_ROUNDS 轮(约 KEEP_ROUNDS×间隔)后彻底遗忘。
MISS_THRESHOLD = int(os.environ.get("MISS_THRESHOLD", "3"))
KEEP_ROUNDS = int(os.environ.get("KEEP_ROUNDS", "24"))
MENU_CACHE = os.path.join(HERE, "menu_cache.json")  # 全菜单 cache (来自 batch_menu.py)
LAT, LNG = "30.195608", "120.260958"
LOCATION = "杭州萧山·花屿观澜里附近3km"
CDP = "http://127.0.0.1:9222"

# ===== 推送消息名称(字母简称) =====
# 微信通知栏只显示标题, 用短代号一眼可辨。想换代号只改这一行即可。
#   PUSH_TAG = "SG-F3K"  -> 标题形如: 【SG-F3K】+3变动 / 【SG-F3K】OK / 【SG-F3K】需过码
# 可用环境变量覆盖: PUSH_TAG=XX python fruit_monitor_core.py
PUSH_TAG = os.environ.get("PUSH_TAG", "SG-F3K")

# ===== 监控范围配置 =====
# 真实距离 distM(米) 才是"3km"的准确依据 —— h5.ele.me 对近处店铺会给出 distM,
# 必须用它对 3km 判定, 否则会严重漏店(实测 ETA=20min 对应物理距离仅 91~409m,
# 之前用 ETA<=15 当 3km 代理等于只监控了约 0.5km, 漏掉大量 3km 内店铺)。
# 仅当 distM 缺失时, 才退化为 ETA(分钟)代理, 放宽到 20min(约 <500m, 稳妥落在 3km 内)。
MAX_DIST_M = int(os.environ.get("MAX_DIST_M", "3000"))       # 真实 3km 半径(米)
MAX_ETA_MIN = int(os.environ.get("MAX_ETA_MIN", "20"))       # distM 缺失时的退化代理(分钟)
# 排除明显非水果店(便利店/超市/药店等), 它们会污染"水果店"监控
NONFRUIT_RE = re.compile(r"(便利店|超市|便利|商城|药店|百货|罗森|全家|联华|十足|菜市场|市集|百世|美宜佳|天猫|小店|杂货|批发|甜品|水牛奶|奶茶|咖啡|烘焙|炸鸡|快餐|火锅|烧烤|熟食|酸奶|YOGURT|yogurt)")

sys.path.insert(0, HERE)
from pushplus_push import push  # noqa


# ---------- 监控范围过滤 ----------
def is_fruit_shop(name):
    """店名是否像水果店(排除便利店/超市等)。"""
    return not bool(NONFRUIT_RE.search(name or ""))


def within_radius(shop, max_dist_m=MAX_DIST_M, max_eta=MAX_ETA_MIN):
    """按真实距离 distM(米) 判定是否在监控半径内(3km); distM 缺失时退化为 ETA 代理。"""
    dist = shop.get("distM")
    if dist not in (None, "", "?"):
        try:
            if int(str(dist).strip()) <= max_dist_m:
                return True
        except Exception:
            pass
    eta = shop.get("etaMin")
    if eta in (None, "", "?"):
        return False
    try:
        return int(str(eta).strip()) <= max_eta
    except Exception:
        return False


def filter_target(shops):
    """仅保留『监控半径内 + 真水果店』, 这是用户要的"3km范围内所有水果店铺"。"""
    return [s for s in (shops or []) if within_radius(s) and is_fruit_shop(s.get("name", ""))]

# ---------- SKU 身份指纹 ----------
def sku_key(sku):
    """SKU稳定身份: 规范化img url + 截断desc。img可能带尺寸参数/时间戳,取主图路径指纹。"""
    img = (sku.get("img") or "").strip()
    # 去掉末尾 _180x180q75_ / _xxx 尺寸后缀 与 query
    m = re.match(r"(https?://[^?]+?)(?:_\d+x\d+[a-z0-9_]*)?\.(jpg|jpeg|png|webp)(\?.*)?$", img, re.I)
    if m:
        img_key = m.group(1) + "." + m.group(2)
    else:
        img_key = img.split("?")[0]
    desc = (sku.get("desc") or "").strip()
    return f"{img_key}|{desc[:60]}"


def _fullmenu_key(sku):
    """全菜单 SKU 的稳定身份(用 name + price + tag/sales, 因为全菜单没 img)。"""
    name = (sku.get("name") or "").strip()[:60]
    price = sku.get("price") or sku.get("price_cn") or ""
    return f"FM|{name}|{price}"


def load_menu_cache(max_age_hours=48):
    """从 menu_cache.json 读全菜单 SKU, 合并到 shops 列表(追加 skus_full 字段)。

    返回 dict: {店名: {"skus_full": [...], "ts": ...}, ...}
    只采纳 max_age_hours 内的项(防 stale 数据污染)。
    """
    if not os.path.exists(MENU_CACHE):
        return {}
    try:
        with open(MENU_CACHE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        return {}
    out = {}
    now = time.time()
    for name, v in raw.items():
        ts = v.get("ts", 0)
        if max_age_hours and (now - ts) > max_age_hours * 3600:
            continue
        skus = v.get("skus") or []
        out[name] = {"skus_full": skus, "ts": ts, "n": v.get("n", len(skus))}
    return out


def merge_fullmenu(shops, fm_cache):
    """把全菜单 cache 合并到 shops 列表(加 skus_full 字段)。"""
    for s in shops:
        name = s.get("name", "")
        if name in fm_cache:
            fm = fm_cache[name]["skus_full"]
            s["skus_full"] = fm
            s["_fullmenu_ts"] = fm_cache[name]["ts"]
    return shops


def normalize_num(s):
    s = (s or "").strip()
    s = s.replace(",", "")
    try:
        return float(s)
    except Exception:
        return None

# ---------- 抓取 ----------
def scrape():
    """返回 shops:[{name,logo,distM,etaMin,rating,monthly,minOrder,deliveryFee,promotions,skus:[{img,desc,tag,price,sales}]}]"""
    from playwright.sync_api import sync_playwright
    JS = r"""() => {
        const ws=[];
        document.querySelectorAll('[class*="mat_shopmode-shop-dragfood-wrapper"]').forEach(w=>{
            let hp=false,pe=w.parentElement;
            while(pe){ if(/mat_shopmode-shop-dragfood-wrapper/.test(pe.className||'')){hp=true;break;} pe=pe.parentElement; }
            if(hp) return;
            ws.push(w);
        });
        const out=[];
        ws.forEach(w=>{
            const card=w.querySelector('[class*="mat_shopmode-shop-item"]');
            if(!card) return;
            const rect=card.getBoundingClientRect();
            // 放宽: 之前 <300 会把窄卡(部分店)误删, 导致只抓到5家而非全部。放宽到 <150。
            if(rect.width<150) return;
            let name='';
            const rich=card.querySelector('tiga-rich-text[nodes]');
            if(rich){
                try{const n=JSON.parse(rich.getAttribute('nodes'));const ts=[];function wk(x){if(!x)return;if(typeof x.text==='string')ts.push(x.text);if(Array.isArray(x.children))x.children.forEach(wk);}n.forEach(wk);name=ts.join('').trim();}catch(e){}
            }
            const txt=(card.innerText||'').trim();
            const distM=(txt.match(/(\d+)\s*m\b/)||[])[1]||'';
            const etaMin=(txt.match(/(\d+)\s*分钟/)||[])[1]||'';
            const rating=(txt.match(/([0-9]\.[0-9])\s*分/)||[])[1]||'';
            const monthly=(txt.match(/月售\s*(\d+\+?)/)||[])[1]||'';
            const minOrder=(txt.match(/起送\s*[¥￥]\s*([0-9]+(?:\.[0-9]+)?)/)||[])[1]||'';
            const deliveryFee=(txt.match(/免配送费\s*[¥￥]\s*([0-9]+(?:\.[0-9]+)?)/)||[])[1]||'';
            const logoImg=card.querySelector('tiga-image');
            const logo=logoImg?(logoImg.getAttribute('src')||''):'';
            const skus=[];
            const ITEM_RE=/(^|\s)shopmode-dragfood-item(\s|$)/;
            w.querySelectorAll('*').forEach(it=>{
                if(!ITEM_RE.test(it.className||'')) return;
                const im=it.querySelector('tiga-image[class*="shop-image"]');
                const img=im?(im.getAttribute('src')||im.getAttribute('lazy-src')||''):'';
                let desc='';
                const drich=it.querySelector('tiga-rich-text[class*="shopmode-food-item-des"]');
                if(drich){ try{const n=JSON.parse(drich.getAttribute('nodes'));const ts=[];function wk(x){if(!x)return;if(typeof x.text==='string')ts.push(x.text);if(Array.isArray(x.children))x.children.forEach(wk);}n.forEach(wk);desc=ts.join('').trim();}catch(e){} }
                const tagEl=it.querySelector('[class*="shopmode-food-item-activityTag"]');
                const tag=tagEl?(tagEl.innerText||'').trim():'';
                const priceEl=it.querySelector('[class*="shopmode-food-item-price-new"]');
                let price='';
                if(priceEl){ const p=priceEl.innerText||''; const m=p.match(/[¥￥]?\s*([0-9]+(?:\.[0-9]+)?)/); if(m) price=m[1]; }
                const sales=(it.innerText||'').match(/已售\s*(\d+\+?)/);
                skus.push({img, desc:desc.slice(0,80), tag, price, sales:sales?sales[1]:''});
            });
            const seen=new Set(); const uniq=[];
            for(const s of skus){ const k=s.img+'|'+s.desc; if(seen.has(k))continue; seen.add(k); uniq.push(s); }
            out.push({name:name||'(无店名)', logo, distM, etaMin, rating, monthly, minOrder, deliveryFee, skus:uniq});
        });
        return out;
    }"""
    def _try_capture(page):
        """滚屏后 evaluate 抓店卡; 若页面是错误墙/空态返回 None 让上层重试恢复。

        增强:
          1) **JS scrollTop 触发分页加载**(饿了么分页不响应 mouse.wheel)
          2) 对每个店铺的 SKU 容器用 JS 设置 scrollLeft, 把"左滑筛选"后面隐藏的 SKU 全滚出来
             (避免 mouse drag 触发点击跳转)
        """
        # —— 1) 多轮「scrollTop 极值触底 + 回顶」, 触发饿了么虚拟列表分页 ——
        # 关键: ele 用 scroll-view 虚拟滚动, 必须设 scrollTop=1e9(超大值)才能触底,
        #       然后回 0 让列表重新渲染前面卡; 反复循环才会持续加载更多店。
        def _scroll_to(page, pos):
            try:
                page.evaluate("""(pos)=>{
                    document.querySelectorAll('[class*=\"search-result-content-wrapper\"], [class*=\"scroll-view\"]').forEach(el=>{
                        try{ el.scrollTop = pos; }catch(e){}
                    });
                    window.scrollTo(0, pos);
                }""", pos)
            except Exception:
                pass

        prev_n = 0
        stable = 0
        # 注意: 该定位点饿了么真实返回上限约 25 家(底部出现"没有更多了"),
        # 滚太多轮只是白白激化 baxia 风控。10 轮 + 连续3轮无新增即停, 足够加载全部。
        for r in range(10):
            _scroll_to(page, 10**9)      # 触底
            page.wait_for_timeout(600)
            _scroll_to(page, 0)          # 回顶, 触发 re-render
            page.wait_for_timeout(450)
            # 中途采集已加载数量, 判断是否已稳定
            try:
                n = page.evaluate("() => document.querySelectorAll('[class*=\"mat_shopmode-shop-dragfood-wrapper\"]').length")
            except Exception:
                n = 0
            if n == prev_n and n > 0:
                stable += 1
                if stable >= 3:  # 连续 3 轮无新增, 认为已加载完
                    print(f"[scroll] 连续 {stable} 轮无新增, 停止于 {n} 家 (r={r})")
                    break
            else:
                stable = 0
            prev_n = n
        page.wait_for_timeout(800)
        # 最后回顶, 让 evaluate 抓到所有店铺卡
        _scroll_to(page, 0)
        page.wait_for_timeout(1000)

        # —— JS 程序化水平滚动每个店铺的 SKU 容器(避免触发点击导航) ——
        for _ in range(5):  # 多轮, 每轮都把每个容器滚到当前最大 scrollLeft
            try:
                page.evaluate(r"""() => {
                    const wrappers = [...document.querySelectorAll('[class*="mat_shopmode-shop-dragfood-wrapper"]')];
                    wrappers.forEach(w=>{
                        // 顶层 wrapper 过滤
                        let hp=false,pe=w.parentElement;
                        while(pe){ if(/mat_shopmode-shop-dragfood-wrapper/.test(pe.className||'')){hp=true;break;} pe=pe.parentElement; }
                        if(hp) return;
                        // 找横向可滚容器(找所有 shopmode-dragfood-item 的共同祖先, scrollWidth > clientWidth)
                        const items = w.querySelectorAll('[class*="shopmode-dragfood-item"]');
                        if (!items.length) return;
                        // 多个候选: items[0] 的父链
                        let sc = items[0].parentElement;
                        const seen = new Set();
                        while (sc && sc !== document.body && !seen.has(sc)) {
                            seen.add(sc);
                            if (sc.scrollWidth > sc.clientWidth + 4) {
                                // 滚到最右
                                sc.scrollLeft = sc.scrollWidth;
                                break;
                            }
                            sc = sc.parentElement;
                        }
                    });
                }""")
            except Exception:
                pass
            page.wait_for_timeout(450)
        # 最后一轮: 全部容器也回到 0(让 evaluate 时 DOM 全部已渲染)
        try:
            page.evaluate(r"""() => {
                document.querySelectorAll('[class*="mat_shopmode-shop-dragfood-wrapper"]').forEach(w=>{
                    let hp=false,pe=w.parentElement;
                    while(pe){ if(/mat_shopmode-shop-dragfood-wrapper/.test(pe.className||'')){hp=true;break;} pe=pe.parentElement; }
                    if(hp) return;
                    const items = w.querySelectorAll('[class*="shopmode-dragfood-item"]');
                    if (!items.length) return;
                    let sc = items[0].parentElement, seen=new Set();
                    while (sc && sc !== document.body && !seen.has(sc)) {
                        seen.add(sc);
                        if (sc.scrollWidth > sc.clientWidth + 4) {
                            sc.scrollLeft = 0;
                            break;
                        }
                        sc = sc.parentElement;
                    }
                });
            }""")
        except Exception:
            pass
        page.wait_for_timeout(700)

        try:
            shops = page.evaluate(JS)
        except Exception:
            return None
        return shops

    with sync_playwright() as p:
        headless = os.environ.get("HEADLESS") == "1"
        if headless:
            # —— 云端模式(GitHub Actions): 自起无头 chromium, 注入饿了么登录 cookie ——
            # 这样云端 runner 无需本地 Chrome, 也不依赖你的 Mac 是否开机。
            b = p.chromium.launch(headless=True,
                                  args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"])
            ctx = b.new_context(
                viewport={"width": 414, "height": 896},
                user_agent=("Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
                            "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 "
                            "Mobile/15E148 Safari/604.1"),
            )
            cookies_json = os.environ.get("ELEME_COOKIES", "")
            if cookies_json:
                try:
                    ck = json.loads(cookies_json)
                    ctx.add_cookies(ck)
                    print(f"[headless] 已注入 {len(ck)} 个饿了么 cookie")
                except Exception as e:
                    print("[headless] cookie 注入失败:", e)
            page = ctx.new_page()
            try:
                page.goto("https://h5.ele.me/minisearch/result?keyword=%E6%B0%B4%E6%9E%9C"
                          "&__locLat=30.195608&__locLng=120.260958&__geohash=wtmeb8fu1j93",
                          timeout=30000, wait_until="domcontentloaded")
            except Exception as e:
                print("[headless] 初始导航异常(继续尝试):", e)
            page.wait_for_timeout(3000)
        else:
            # —— 本地模式: 复用常驻 Chrome(CDP 9222) ——
            b = p.chromium.connect_over_cdp(CDP, timeout=30000)
            page = b.contexts[0].pages[0]
            # 确保当前页是水果结果页(不含则导航)。URL 可能含 login 跳转,兜底处理。
            url = page.url
            if "minisearch" not in url or "keyword=%E6%B0%B4%E6%9E%9C" not in url:
                try:
                    page.goto("https://h5.ele.me/minisearch/result?keyword=%E6%B0%B4%E6%9E%9C"
                              "&__locLat=30.195608&__locLng=120.260958&__geohash=wtmeb8fu1j93",
                              timeout=25000, wait_until="domcontentloaded")
                except Exception:
                    pass
        # 自愈: 最多 N 轮 —— 若页面错误墙/空态,点"重新加载"恢复后重抓。
        # 注意: 检测到验证码(punish)时**不再重试 goto/重载**, 也不再点"重新加载"(会激化风控),
        #       而是停下来等待人工通过; 若超时仍未通过, 本轮直接返回空(上层推"需人工过验证码"提示)。
        shops = None
        captcha_seen = False
        captcha_still = False
        for attempt in range(3):
            # 检测当前是否错误墙(出错了/检修中) 或验证码(punish/选图)
            state = None
            try:
                state = page.evaluate(r"""() => {
                    const t = document.body ? document.body.innerText : '';
                    // 关键: baxia iframe 过码后常留在 DOM 但折叠为 0x0, 不能只看"存在", 必须判"可见(宽高>0)"
                    function blocking(sel){
                        const els = document.querySelectorAll(sel);
                        for(const el of els){
                            const r = el.getBoundingClientRect();
                            if(r.width>0 && r.height>0){
                                const cs = getComputedStyle(el);
                                if(cs.visibility!=='hidden' && cs.display!=='none') return true;
                            }
                        }
                        return false;
                    }
                    const punish = blocking('iframe[src*="punish"]') || blocking('#baxia-dialog-content') || blocking('[class*="captcha"]');
                    const err = /出错了|检修中/.test(t) && !/[¥￥]月售/.test(t);
                    const hasShop = !!document.querySelector('[class*="mat_shopmode-shop-dragfood-wrapper"]');
                    return {err, punish, hasShop, head: t.slice(0, 40)};
                }""")
            except Exception:
                state = {"err": True, "punish": False, "hasShop": False, "head": "(nav)"}

            # —— 若出现验证码: 等待人工通过(最长 100s, 每 2s 探测), 期间**不触发任何请求** ——
            if state.get("punish"):
                captcha_seen = True
                print("[captcha] 检测到验证码, 等待人工通过 (最长100s, 低频模式不刷新)...")
                cleared = False
                for _ in range(50):
                    page.wait_for_timeout(2000)
                    try:
                        p2 = page.evaluate(r"""() => {
                            // 同 state 判定: 只在 baxia 弹窗"真正可见"时算未通过(过码后常残留 0x0 隐藏 iframe)
                            function blocking(sel){
                                const els = document.querySelectorAll(sel);
                                for(const el of els){
                                    const r = el.getBoundingClientRect();
                                    if(r.width>0 && r.height>0){
                                        const cs = getComputedStyle(el);
                                        if(cs.visibility!=='hidden' && cs.display!=='none') return true;
                                    }
                                }
                                return false;
                            }
                            return blocking('iframe[src*="punish"]') || blocking('#baxia-dialog-content') || blocking('[class*="captcha"]');
                        }""")
                    except Exception:
                        p2 = False
                    if not p2:
                        cleared = True
                        break
                if not cleared:
                    captcha_still = True
                    print("[captcha] 100s内未通过, 本轮放弃抓取(不激化风控)")
                    break  # 不重试, 避免反复触发
                print("[captcha] 验证码已通过, 等待页面稳定后重抓")
                page.wait_for_timeout(2500)

            # 直接尝试抓(即使 err 也试,可能部分渲染)
            shops = _try_capture(page)
            if shops:
                break

            # —— 抓到空: 仅在**非验证码**状态下才点"重新加载"(最多1次), 避免激化风控 ——
            if captcha_seen and attempt >= 1:
                print("[self-heal] 验证码刚通过仍未出店, 本轮暂停(不再强刷)")
                break
            try:
                r = page.evaluate(r"""() => {
                    const els=[...document.querySelectorAll('div,span,tiga-view,button')];
                    for(const el of els){
                        const t=(el.innerText||'').trim();
                        if(t==='重新加载' && el.offsetParent!==null){
                            const rc=el.getBoundingClientRect();
                            return {x:rc.x+rc.width/2, y:rc.y+rc.height/2};
                        }
                    }
                    return null;
                }""")
                if r:
                    page.mouse.click(r["x"], r["y"])
                    print(f"[self-heal] 点击\"重新加载\" (attempt {attempt+1})")
                    page.wait_for_timeout(5000)
            except Exception:
                pass
        if captcha_seen:
            print("[captcha] 本轮曾出现验证码" + ("(未通过)" if captcha_still else ""))
        b.close()
    # 返回 (店铺列表, 是否遇到验证码未通过)
    return shops or [], captcha_still


# ---------- 对比引擎 ----------
def _mkey_shop(name):
    return "S|" + (name or "")


def _mkey_sku(name, k):
    return "K|" + (name or "") + "|" + (k or "")


def load_prev_raw():
    """读整份基线(dict: {_ts, location, shops, _miss, _reported, ...})。不存在/损坏返回 None。"""
    if not os.path.exists(BASELINE):
        return None
    try:
        with open(BASELINE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def load_prev():
    """兼容旧接口: 只返回基线里的 shops 列表(含影子条目)。"""
    d = load_prev_raw()
    return d.get("shops") if d else None


def merge_baseline(prev, new, miss, reported):
    """把本轮 new 与上一轮基线 prev 合并成新基线(供下轮对比)。

    - 当前店: 用新数据, 但把"上轮有、本轮没抓到"且未超保留期的 SKU 作为影子项带上(去抖用)
    - 本轮未出现的店: 整条作为影子项保留(_shadow=True), 连续缺失超过 KEEP_ROUNDS 轮才彻底遗忘
    """
    import copy
    prev_shops = {s.get("name", ""): s for s in (prev or [])}
    new_names = {s.get("name", "") for s in (new or [])}
    out = []
    for s in (new or []):
        s = copy.deepcopy(s)
        name = s.get("name", "")
        p = prev_shops.get(name)
        if p:
            nk = {sku_key(x) for x in s.get("skus", [])}
            shadow = [x for x in p.get("skus", [])
                      if sku_key(x) not in nk
                      and miss.get(_mkey_sku(name, sku_key(x)), 0) <= KEEP_ROUNDS]
            s["skus"] = list(s.get("skus", [])) + copy.deepcopy(shadow)
            if not s.get("skus_full") and p.get("skus_full"):
                s["skus_full"] = copy.deepcopy(p["skus_full"])
        s.pop("_shadow", None)
        s.pop("_miss", None)
        out.append(s)
    for name, p in prev_shops.items():
        if name in new_names:
            continue
        c = miss.get(_mkey_shop(name), 0)
        if c > KEEP_ROUNDS:      # 连续太久没出现, 彻底遗忘(不再占用基线)
            continue
        q = copy.deepcopy(p)
        q["_shadow"] = True
        q["_miss"] = c
        out.append(q)
    return out


def diff(prev, new, miss=None, reported=None, threshold=MISS_THRESHOLD):
    """对比 prev/new 两家店列表。

    返回 (变动清单 list[str], 汇总 dict, miss dict, reported set)。

    去抖规则(2026-09-06 新增, 根治假报):
      · 店铺/SKU 必须**连续 threshold 轮**未出现, 才判定"消失/下架";
      · 同一条目只播报一次(记入 reported), 之后持续缺失不再刷屏;
      · 抖动恢复(未播报过就又出现)静默处理, 不产生噪音。
    """
    miss = dict(miss or {})
    reported = set(reported or [])
    touched = set()
    out = []
    stat = {"up": 0, "down": 0, "new_sku": 0, "removed_sku": 0,
            "new_shop": 0, "gone_shop": 0, "back_shop": 0, "back_sku": 0}

    prev_shops = {s.get("name", ""): s for s in (prev or [])}
    new_shops = {s.get("name", ""): s for s in (new or [])}
    prev_names = set(prev_shops)
    new_names = set(new_shops)

    # 1) 新店 / 店铺回归
    for n in sorted(new_names - prev_names):
        stat["new_shop"] += 1
        s = new_shops[n]
        out.append(f"🆕 新店：{n}（{s.get('distM','?')}m · {s.get('etaMin','?')}min · ★{s.get('rating','?')} · 月售{s.get('monthly','?')}）")
    for n in sorted(prev_names & new_names):
        k = _mkey_shop(n)
        miss.pop(k, None)
        if k in reported:            # 之前播报过"消失", 现在又出现了 → 回归
            reported.discard(k)
            stat["back_shop"] += 1
            out.append(f"🔄 店铺回归：{n}")

    # 2) 店铺消失(连续 threshold 轮未见才报, 且只报一次)
    for n in sorted(prev_names - new_names):
        k = _mkey_shop(n)
        c = miss.get(k, 0) + 1
        miss[k] = c
        touched.add(k)
        if c >= threshold and k not in reported:
            reported.add(k)
            stat["gone_shop"] += 1
            out.append(f"🈚 店铺消失：{n}（连续 {c} 轮未出现）")

    # 3) 同名店铺的 SKU 对比
    for n in sorted(prev_names & new_names):
        ps = {sku_key(s): s for s in prev_shops[n].get("skus", [])}
        ns = {sku_key(s): s for s in new_shops[n].get("skus", [])}
        short = n[:20] + ("…" if len(n) > 20 else "")
        # 3a) 新上架 / 重新上架
        for k in sorted(ns):
            mk = _mkey_sku(n, k)
            if k in ps:
                miss.pop(mk, None)
                if mk in reported:   # 之前播报过"下架", 现在回来了
                    reported.discard(mk)
                    stat["back_sku"] += 1
                    out.append(f"🔄[{short}] 重新上架 ¥{ns[k].get('price')} {_sku_short(ns[k])}")
                continue
            stat["new_sku"] += 1
            out.append(f"🆕[{short}] 新上架 ¥{ns[k].get('price')} {_sku_short(ns[k])}")
        # 3b) 下架(连续 threshold 轮未见才报, 且只报一次)
        for k in sorted(ps):
            if k in ns:
                continue
            mk = _mkey_sku(n, k)
            c = miss.get(mk, 0) + 1
            miss[mk] = c
            touched.add(mk)
            if c >= threshold and mk not in reported:
                reported.add(mk)
                stat["removed_sku"] += 1
                out.append(f"❌[{short}] 已下架 ¥{ps[k].get('price')} {_sku_short(ps[k])}（连续 {c} 轮未见）")
        # 3c) 价格变动
        for k in sorted(set(ps) & set(ns)):
            pv = normalize_num(ps[k].get("price"))
            nv = normalize_num(ns[k].get("price"))
            if pv is None or nv is None or pv == nv:
                continue
            d = nv - pv
            if d > 0:
                stat["up"] += 1
                out.append(f"📈[{short}] 涨价 {ps[k].get('price')}→{ns[k].get('price')}（{_sku_short(ns[k])}）")
            else:
                stat["down"] += 1
                out.append(f"📉[{short}] 降价 {ps[k].get('price')}→{ns[k].get('price')}（{_sku_short(ns[k])}）")

    # 4) 计数器瘦身: 本轮没碰到的(对应条目已回归或已遗忘)直接丢弃
    miss = {k: v for k, v in miss.items() if k in touched}
    reported = {k for k in reported if k in miss}
    return out, stat, miss, reported


def _sku_short(sk):
    d = (sk.get("desc") or "").strip()
    if not d:
        d = f"(图{sk.get('img','')[-20:]})"
    return d[:34]


# ---------- 消息构造 ----------
def build_message(shops, changes, stat, meta):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    L = []
    L.append(f"🍎 3km 水果店实时监控 · {meta}")
    L.append(f"🕐 {now} 推送（每10分钟）")
    L.append("")
    # 概览
    total_sku = sum(len(s.get("skus", [])) for s in shops)
    n_full = sum(1 for s in shops if s.get("skus_full"))
    total_full_sku = sum(len(s.get("skus_full", [])) for s in shops)
    mode_note = f" · {n_full} 家全菜单({total_full_sku} 款)" if n_full else ""
    L.append(f"📍 当前 {len(shops)} 家店 · 搜索页 SKU {total_sku} 个{mode_note}")
    # 变动汇总
    ch = stat
    n_back = ch.get("back_shop", 0) + ch.get("back_sku", 0)
    if sum(ch.values()) > 0:
        L.append(f"【变动】涨{ch['up']} · 降{ch['down']} · 上新{ch['new_sku']} · 下架{ch['removed_sku']}"
                 f" · 新店{ch['new_shop']} · 消失{ch['gone_shop']}"
                 + (f" · 回归{n_back}" if n_back else ""))
    else:
        L.append("【变动】与上次一致，无涨跌/上下架/店铺变化")
    L.append("")
    if changes:
        L.append("— 明细 —")
        for c in changes[:30]:
            L.append("• " + c)
        if len(changes) > 30:
            L.append(f"… 另有 {len(changes)-30} 条")
        L.append("")
    # 店铺+SKU清单
    L.append("— 当前店铺与SKU —")
    for i, s in enumerate(shops, 1):
        full = s.get("skus_full")
        n_full_menu = len(full) if full else 0
        mode_tag = f"  · 全菜单{n_full_menu}款" if full else ""
        p = [sk.get("price") for sk in s.get("skus", [])]
        L.append(f"{i}. {s['name']}  ({s.get('distM','?')}m · {s.get('etaMin','?')}min · ★{s.get('rating','?')}"
                 f" · 月售{s.get('monthly','?')} · 搜索页{len(s.get('skus',[]))}款{mode_tag})")
        # 优先显示全菜单 (如果有), 否则显示搜索页 6 款
        if full:
            show = full[:30]  # 单段最多 30 款, 防止 pushplus 单条超长
            L.append(f"    【全菜单 前 {len(show)}/{len(full)} 款】")
            for sk in show:
                t = (sk.get("tag") or "")
                tag = f"[{t}] " if t else ""
                L.append(f"      ¥{sk.get('price_cn','?'):<8} {tag}{(sk.get('name') or '')[:34]}")
            if len(full) > 30:
                L.append(f"    （剩余 {len(full)-30} 款已存本地存档 fruit_full_*.json）")
        else:
            for sk in s.get("skus", []):
                t = (sk.get("tag") or "")
                tag = f"[{t}] " if t else ""
                L.append(f"    ¥{sk.get('price','?'):<6} {tag}{_sku_short(sk)}")
    L.append("")
    L.append("⚠️ 数据来自 h5.ele.me 实时抓取，价格含预估/活动价，仅供参考。")
    return "\n".join(L)


# ---------- 主循环 ----------
def run_once(push_msg=True, use_prev=True):
    """执行一轮: 抓取 → 对比上次 → 保存baseline → 推送。返回 (ok, message, stat)。"""
    token = os.environ.get("PUSHPLUS_TOKEN", "a4b4bacfd0544983b604f36539450211")
    prev_raw = load_prev_raw() if use_prev else None
    prev = (prev_raw or {}).get("shops")
    miss = (prev_raw or {}).get("_miss") or {}
    reported = set((prev_raw or {}).get("_reported") or [])
    if prev_raw:
        n_shadow = sum(1 for s in (prev or []) if s.get("_shadow"))
        print(f"[baseline] {os.path.basename(BASELINE)} · 上轮 {len(prev or [])} 家(影子{n_shadow}) "
              f"· miss计数器 {len(miss)} · 第 {((prev_raw or {}).get('_rounds') or 0)+1} 轮")

    ts_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        res = scrape()
        # 兼容: scrape() 现返回 (shops, captcha_still)
        if isinstance(res, tuple):
            new_shops, captcha_still = res
        else:
            new_shops, captcha_still = res, False
        # 合并全菜单 cache (进店抓的 50+ 款 SKU)
        fm_cache = load_menu_cache(max_age_hours=72)
        new_shops = merge_fullmenu(new_shops or [], fm_cache)
        print(f"[merge] 全菜单 cache: {len(fm_cache)} 家店, 合并进 {sum(1 for s in (new_shops or []) if s.get('skus_full'))} 家")

        # —— 关键: 只保留 3km 内真水果店 (用户要的是"3km范围内所有水果店铺", 而非搜索返回的355家全量) ——
        raw_shops = list(new_shops or [])
        raw_count = len(raw_shops)
        new_shops = filter_target(raw_shops)
        print(f"[filter] 原始 {raw_count} 家 → 3km内水果店 {len(new_shops)} 家 (MAX_ETA_MIN={MAX_ETA_MIN})")
    except Exception as e:
        # 抓取失败: 若push_msg则推送异常提示
        print(f"SCRAPE ERROR: {e!r}")
        if push_msg:
            ok, det = push(token, "⚠️【监控异常】萧山水果店抓取失败",
                           f"{ts_str}\n{type(e).__name__}: {e}\n\n浏览器/CDP可能未开启或需过验证码。", template="txt")
            print(("ERR-PUSH " if ok else "ERR-PUSH-FAIL ") + det)
        return False, f"scrape failed: {e}", {}

    # 正常抓取到(可能0店=空,可能页面错误)
    if not new_shops:
        if captcha_still:
            body = (ts_str + "\n⚠️ 饿了么弹出图形验证码，需人工在浏览器里过一下后我才能继续抓取。\n"
                            "（请打开监控用的 Chrome 窗口，完成图片验证后点提交，下个周期我会自动恢复）")
            print("SCRAPE 0 shops (captcha)")
            if push_msg:
                ok, det = push(token, f"【{PUSH_TAG}】CAP 需人工过码", body, template="txt")
                print(("CAPTCHA-PUSH " if ok else "CAPTCHA-PUSH-FAIL ") + det)
            return False, "captcha", {}
        # 过滤后 0 家目标店: 推极简状态(附全量店数 + 最近店ETA), 保证每10分钟都有回执但不刷屏
        etas = [int(s["etaMin"]) for s in (raw_shops or []) if s.get("etaMin") and str(s["etaMin"]).isdigit()]
        min_eta = min(etas) if etas else "?"
        near = sorted([s for s in (raw_shops or []) if s.get("etaMin") and str(s["etaMin"]).isdigit()],
                      key=lambda x: int(x["etaMin"]))[:3]
        body = (f"{ts_str}\n"
                f"3km内(ETA≤{MAX_ETA_MIN}min) 暂无水果店。\n"
                f"搜索返回 {len(raw_shops or [])} 家, 最快 ETA {min_eta}min。\n")
        if near:
            body += "最近的店:\n" + "\n".join(f"  · {s['etaMin']}min {s['name'][:26]}" for s in near)
        print(f"SCRAPE 0 target shops (raw={len(raw_shops or [])}, min_eta={min_eta})")
        if push_msg:
            ok, det = push(token, f"【{PUSH_TAG}】暂无3km店(最快{min_eta}min)", body, template="txt")
            print(("ZERO-PUSH " if ok else "ZERO-PUSH-FAIL ") + det)
        return True, "0 target shops", {}

    changes, stat, miss, reported = diff(prev, new_shops, miss, reported)

    # 保存本次为新的 baseline (供下次对比) —— 仅存过滤后的监控目标集 + 未超期的影子条目,
    # 保证 diff 口径一致, 同时给"抖动未抓到"的店/SKU 留下缓冲, 避免假报消失/下架。
    baseline_shops = merge_baseline(prev, new_shops, miss, reported)
    snap = {"_ts": time.time(), "_captured_at": ts_str, "location": LOCATION,
            "radius_eta_max": MAX_ETA_MIN, "baseline": os.path.basename(BASELINE),
            "shops": baseline_shops, "_miss": miss, "_reported": sorted(reported),
            "_rounds": ((prev_raw or {}).get("_rounds") or 0) + 1,
            "_visible": len(new_shops)}
    with open(BASELINE, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)

    meta = "花屿观澜里附近3km"
    content = build_message(new_shops, changes, stat, meta)

    # 存完整数据(本地存档, 永不丢失; 即使 pushplus 超长被截断也不丢)。raw=全量, filtered=监控目标
    full_path = os.path.join(HIST, f"fruit_full_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
    try:
        with open(full_path, "w", encoding="utf-8") as f:
            json.dump({"_ts": time.time(), "location": LOCATION, "raw_shops": raw_count,
                       "target_shops": len(new_shops), "shops": raw_shops}, f, ensure_ascii=False, indent=2)
    except Exception:
        full_path = None

    if push_msg:
        # 分段推送: 避免单条超长被 pushplus 拒绝 (code=999)。监控目标经 3km 过滤后通常 <40 家, 一般单条即可。
        chunk = 20
        shops_list = new_shops
        total = max(1, (len(shops_list) + chunk - 1) // chunk)
        n_changes = sum(stat.values())
        for ci in range(total):
            seg = shops_list[ci * chunk:(ci + 1) * chunk]
            seg_changes = changes if ci == 0 else []  # 变动明细只放第一段
            seg_content = build_message(seg, seg_changes, stat, meta)
            if ci == 0:
                # 标题用字母简称 + 变动数(微信通知栏一眼可辨)
                tail = f"N+{n_changes}" if n_changes else "OK"
                seg_title = f"【{PUSH_TAG}】{ci+1}/{total} {tail}"
            else:
                seg_title = f"【{PUSH_TAG}】{ci+1}/{total}"
            if full_path:
                seg_content += f"\n\n📁 完整数据(含全量355家原始)已存: {os.path.basename(full_path)}"
            ok, det = push(token, seg_title, seg_content, template="txt")
            print(("PUSH OK: " if ok else "PUSH FAIL: ") + det)
            if total > 1:
                time.sleep(2.0)  # 多段之间留间隔, 避免 pushplus 频率限制 (code=999)
    else:
        print("push_msg=False, skip push")
    return True, content, stat


if __name__ == "__main__":
    ok, msg, stat = run_once(push_msg=True)
    print("\n========= 消息预览 =========")
    print(msg)
