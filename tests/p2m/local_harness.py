"""Local, mocked-API harness for Drone Locator static UI checks.
Serves web/ on 127.0.0.1:8765; API (http://127.0.0.1:8091) is mocked by Playwright routes.
Usage: python local_harness.py <outprefix> [locked|unlocked|signedout]
"""
import asyncio, json, sys, os, subprocess, time
from playwright.async_api import async_playwright
ROOT = os.path.join(os.path.dirname(__file__), "..", "..", "web")
OUT = os.path.join(os.path.dirname(__file__), "..", "..", "screenshots", "p2m")
os.makedirs(OUT, exist_ok=True)
PHOTO = "/workspace/lostdrone/photos/DJI_20261107180521_0037_D.JPG"
CHROME = "/opt/ungoogled-chromium/chrome"
PHONE = dict(viewport={"width": 412, "height": 915}, device_scale_factor=1, is_mobile=True, has_touch=True,
             user_agent="Mozilla/5.0 (Linux; Android 13; Pixel 6) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
DESK = dict(viewport={"width": 1280, "height": 800})

def user(mode):
    if mode == "signedout":
        return {"authenticated": False}
    return {"authenticated": True, "csrf": "x", "user": {"username": "mock", "mosaic_enabled": mode == "unlocked", "is_admin": False}}

async def mock(route, mode, log):
    url = route.request.url
    path = url.split("8091", 1)[1].split("?")[0]
    log.append((route.request.method, path))
    if path == "/auth/me":
        return await route.fulfill(json=user(mode))
    if path == "/health":
        return await route.fulfill(json={"ok": True})
    if path == "/mosaics" and route.request.method == "POST":
        if os.environ.get("MOCK_FAIL"):
            return await route.fulfill(status=500, json={"detail": "Internal Server Error"})
        return await route.fulfill(json={"id": "mock1", "filename": "DJI.JPG", "width": 4000, "height": 2256, "georeferenced": False})
    if path.endswith("/preview"):
        return await route.fulfill(path=PHOTO, content_type="image/jpeg")
    if path == "/feedback":
        await asyncio.sleep(0.4)
        return await route.fulfill(json={"feedback": {"id": 1}})
    return await route.fulfill(status=404, json={"detail": "mock: not found"})

async def page_for(b, ctxkw, mode, log):
    ctx = await b.new_context(**ctxkw)
    await ctx.add_init_script("localStorage.setItem('dm_tutorial_completed','1')")
    await ctx.route("http://127.0.0.1:8091/**", lambda r: mock(r, mode, log))
    pg = await ctx.new_page()
    errs = []
    pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
    pg.on("pageerror", lambda e: errs.append(str(e)))
    await pg.goto("http://127.0.0.1:8765/index.html", wait_until="networkidle")
    await pg.wait_for_timeout(800)
    return ctx, pg, errs

async def main():
    prefix = sys.argv[1]
    mode = sys.argv[2] if len(sys.argv) > 2 else "unlocked"
    srv = subprocess.Popen([sys.executable, "-m", "http.server", "8765", "--bind", "127.0.0.1", "-d", ROOT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
            for name, kw in (("phone412", PHONE), ("desk1280", DESK)):
                log = []
                ctx, pg, errs = await page_for(b, kw, mode, log)
                await pg.screenshot(path=f"{OUT}/{prefix}-{mode}-{name}.png")
                m = await pg.evaluate("""() => {
                  const r = (s) => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return {top: Math.round(b.top), h: Math.round(b.height), w: Math.round(b.width)}; };
                  const small = [...document.querySelectorAll('#viewFind input, #viewFind select, #viewFind button, #viewFind summary, .page-tabs button, .page-tabs a')]
                    .filter(e => e.offsetParent).map(e => { const b = e.getBoundingClientRect(); const lab = e.closest('label'); const lb = lab ? lab.getBoundingClientRect() : b; return {id: e.id || e.type || e.tagName, w: Math.round(Math.max(b.width, lb.width)), h: Math.round(Math.max(b.height, lb.height))}; })
                    .filter(x => x.h < 44 || x.w < 44);
                  return {tabs: r('.page-tabs'), map: r('#findMap'), aside: r('#findApp aside'), lock: r('[data-mosaic-lock]'), small};
                }""")
                print(name, json.dumps(m))
                if mode == "locked" and name == "phone412":
                    btn = pg.locator("#btnRequestAccess")
                    await btn.click()
                    await pg.wait_for_timeout(200)
                    await pg.screenshot(path=f"{OUT}/{prefix}-{mode}-{name}-req1.png", full_page=True)
                    print("after first tap feedback posts:", sum(1 for x in log if x[1] == "/feedback"))
                    conf = pg.locator("[data-lock-request-confirm]")
                    if await conf.count() and await conf.is_visible():
                        await conf.click()
                        await pg.wait_for_timeout(900)
                    await pg.screenshot(path=f"{OUT}/{prefix}-{mode}-{name}-req2.png", full_page=True)
                    print("after confirm feedback posts:", sum(1 for x in log if x[1] == "/feedback"))
                    print("status:", await pg.locator("[data-lock-request-status]").inner_text())
                if mode == "unlocked":
                    await pg.screenshot(path=f"{OUT}/{prefix}-{mode}-{name}-full.png", full_page=True)
                    print("search hint:", await pg.locator("#findSearchHint").inner_text())
                    if name == "phone412":
                        await pg.click("#btnHeaderMenu")
                        await pg.wait_for_timeout(200)
                        await pg.screenshot(path=f"{OUT}/{prefix}-{mode}-{name}-menu.png")
                        await pg.click("#findMap", position={"x": 200, "y": 50})
                        print("menu closes on outside tap:", await pg.locator("#btnHeaderMenu").get_attribute("aria-expanded"))
                    await pg.set_input_files("#fileRef", "/workspace/drone_top.jpg")
                    await pg.wait_for_timeout(300)
                    print("ref status (no map):", await pg.locator("#refStatus").inner_text())
                    await pg.set_input_files("#fileMosaic", PHOTO)
                    await pg.wait_for_timeout(2500)
                    print("after map load: progress=", await pg.locator("#findProgress").inner_text())
                    print("error:", await pg.locator("#findError").inner_text() if await pg.locator("#findError").is_visible() else "(none)")
                    print("search enabled:", await pg.locator("#btnFindSearch").is_enabled(), "| hint visible:", await pg.locator("#findSearchHint").is_visible())
                    print("map box:", await pg.evaluate("(() => { const e = document.querySelector('.find-map-wrap'); const b = e.getBoundingClientRect(); return [Math.round(b.top), Math.round(b.height)]; })()"))
                    await pg.screenshot(path=f"{OUT}/{prefix}-{mode}-{name}-maploaded{'-fail' if os.environ.get('MOCK_FAIL') else ''}.png")
                    print("search title:", await pg.locator("#btnFindSearch").get_attribute("title"), "| hint:", await pg.locator("#findProgress").inner_text())
                print("errors:", errs[:5])
                await ctx.close()
            await b.close()
    finally:
        srv.terminate()

if __name__ == "__main__":
    asyncio.run(main())
