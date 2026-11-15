"""Live check of the re-review fixes with uxreview-dl-20261114 (password read from file, never printed)."""
import asyncio, json, os, sys
from playwright.async_api import async_playwright
sys.path.insert(0, os.path.dirname(__file__))
from local_harness import PHONE, DESK, CHROME, OUT
BASE = "https://wheresthepelican.com/"
def creds(user):
    lines = open("/workspace/uxreview-logins/logins.txt").read().splitlines()
    for i, l in enumerate(lines):
        if l.strip() == f"Username: {user}":
            for m in lines[i + 1:i + 3]:
                if m.strip().startswith("Password:"):
                    return m.split(":", 1)[1].strip()
    raise SystemExit("creds not found")
PW = creds("uxreview-dl-20261114")
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        for name, kw in (("phone412", PHONE), ("desk1280", DESK)):
            ctx = await b.new_context(**kw)
            await ctx.add_init_script("localStorage.setItem('dm_tutorial_completed','1')")
            pg = await ctx.new_page(); errs = []; posts = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("request", lambda r: posts.append(r.url.split("/api")[-1]) if r.method == "POST" else None)
            await pg.goto(BASE + "login/", wait_until="networkidle")
            await pg.fill("input[name=username], #username, input[autocomplete=username]", "uxreview-dl-20261114")
            await pg.fill("input[type=password]", PW)
            await pg.click("button[type=submit]")
            await pg.wait_for_timeout(2500)
            await pg.goto(BASE, wait_until="networkidle"); await pg.wait_for_timeout(1500)
            info = await pg.evaluate("""() => {
              const r = s => { const e = document.querySelector(s); if (!e) return null; const b = e.getBoundingClientRect(); return [Math.round(b.top), Math.round(b.height)]; };
              const mods = performance.getEntriesByType('resource').map(e => e.name).filter(n => /\\/js\\/|app\\.css/.test(n)).map(n => n.split('/').pop());
              return {header: r('.page-tabs'), map: r('.find-map-wrap'), hint: document.querySelector('#findSearchHint')?.innerText, mods: [...new Set(mods)],
                      adminVisible: !!document.querySelector('#authAdminLink')?.offsetParent, lockVisible: !!document.querySelector('[data-mosaic-lock]')?.offsetParent,
                      footer: document.querySelector('#siteFooter')?.innerText};
            }""")
            print(name, json.dumps(info))
            await pg.screenshot(path=f"{OUT}/live-{name}-1-landing.png")
            if name == "phone412":
                await pg.click("#btnHeaderMenu"); await pg.wait_for_timeout(200)
                await pg.screenshot(path=f"{OUT}/live-{name}-2-menu.png")
                await pg.click("#btnHeaderMenu")
            await pg.click("#btnFindDemo"); await pg.wait_for_timeout(9000)
            info2 = await pg.evaluate("""() => ({progress: document.querySelector('#findProgress')?.innerText, error: document.querySelector('#findError')?.hidden ? '' : document.querySelector('#findError')?.innerText,
               map: (() => { const b = document.querySelector('.find-map-wrap').getBoundingClientRect(); return [Math.round(b.top), Math.round(b.height)]; })(),
               matches: document.querySelectorAll('#findResults .candidate').length})""")
            print(name, "demo:", json.dumps(info2), "POSTs:", posts, "errors:", errs[:3])
            await pg.screenshot(path=f"{OUT}/live-{name}-3-demo.png")
            await ctx.close()
        await b.close()
asyncio.run(main())
