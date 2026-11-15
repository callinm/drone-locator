"""Live check of the photo-map UI with uxreview-dl-20261114 (password read from file, never printed)."""
import asyncio, glob, json, os, sys
from playwright.async_api import async_playwright
sys.path.insert(0, os.path.dirname(__file__))
from local_harness import PHONE, DESK, CHROME, OUT
from live_verify import creds, BASE
PW = creds("uxreview-dl-20261114")
PHOTOS = sorted(glob.glob("/workspace/aukerman40-small/*"))[:6]
async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=CHROME, args=["--no-sandbox"])
        for name, kw in (("phone412", PHONE), ("desk1280", DESK)):
            ctx = await b.new_context(**kw)
            await ctx.add_init_script("localStorage.setItem('dm_tutorial_completed','1')")
            pg = await ctx.new_page(); errs = []; reqs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.on("response", lambda r: reqs.append(f"{r.request.method} {r.url.split('/api')[-1]} {r.status}") if "/searchmaps" in r.url else None)
            await pg.goto(BASE + "login/", wait_until="networkidle")
            await pg.fill("input[name=username], #username, input[autocomplete=username]", "uxreview-dl-20261114")
            await pg.fill("input[type=password]", PW)
            await pg.click("button[type=submit]"); await pg.wait_for_timeout(2500)
            await pg.goto(BASE, wait_until="networkidle"); await pg.wait_for_timeout(1500)
            info = await pg.evaluate("""() => {
              const r = s => { const e = document.querySelector(s); if (!e || !e.offsetParent) return null; const b = e.getBoundingClientRect(); return [Math.round(b.top), Math.round(b.height), Math.round(b.width)]; };
              const mods = performance.getEntriesByType('resource').map(e => e.name).filter(n => /\\/js\\//.test(n)).map(n => n.split('/').pop());
              return {btn: r('#btnPhotoMap'), btnText: document.querySelector('#btnPhotoMap')?.innerText, have: r('#btnLoadMosaic'), header: r('.page-tabs'), map: r('.find-map-wrap'),
                      unversioned: mods.filter(m => !m.includes('v=20261114dlpm3')), photomap: mods.filter(m => m.startsWith('photomap'))};
            }""")
            print(name, json.dumps(info))
            await pg.screenshot(path=f"{OUT}/live-pm-{name}-1-step1.png")
            if name == "desk1280":
                await pg.set_input_files("#filePhotoMap", PHOTOS)
                for _ in range(40):
                    await pg.wait_for_timeout(1000)
                    st = await pg.evaluate("() => ({status: document.querySelector('#photoMapStatus')?.innerText, retry: !!document.querySelector('#btnPhotoMapRetry')?.offsetParent, cancel: !!document.querySelector('#btnPhotoMapCancel')?.offsetParent, steps: [...document.querySelectorAll('[data-pm-step]')].map(e => e.dataset.pmStep + ':' + e.className).join(' ')})")
                    if any(k in (st['status'] or '') for k in ("already have", "Success", "went wrong", "could not")) or "/start" in " ".join(reqs):
                        await pg.wait_for_timeout(1500); break
                st = await pg.evaluate("() => ({status: document.querySelector('#photoMapStatus')?.innerText, err: document.querySelector('#findError')?.hidden ? '' : document.querySelector('#findError')?.innerText, cancel: !!document.querySelector('#btnPhotoMapCancel')?.offsetParent})")
                print(name, "after upload:", json.dumps(st))
                await pg.screenshot(path=f"{OUT}/live-pm-{name}-2-upload.png")
                if st["cancel"]:
                    pg.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
                    await pg.click("#btnPhotoMapCancel"); await pg.wait_for_timeout(2500)
                    await pg.screenshot(path=f"{OUT}/live-pm-{name}-3-cancelled.png")
            print(name, "searchmap requests:", reqs, "errors:", errs[:3])
            await ctx.close()
        await b.close()
asyncio.run(main())
