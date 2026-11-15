import asyncio, sys, subprocess, time
sys.path.insert(0, "tests/p2m")
import local_harness as H
from playwright.async_api import async_playwright
async def main():
    srv = subprocess.Popen([sys.executable, "-m", "http.server", "8765", "--bind", "127.0.0.1", "-d", H.ROOT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); time.sleep(0.8)
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path=H.CHROME, args=["--no-sandbox"])
        for mode in ("signedout", "unlocked"):
            ctx = await b.new_context(**H.PHONE); log=[]
            await ctx.route("http://127.0.0.1:8091/**", lambda r: H.mock(r, mode, log))
            pg = await ctx.new_page()
            await pg.goto("http://127.0.0.1:8765/index.html?tour=1", wait_until="networkidle"); await pg.wait_for_timeout(800)
            seen=[]
            for i in range(9):
                t = await pg.evaluate("(document.querySelector('#tutorialHost')||{}).innerText||''")
                seen.append((t.split('\n')[0:2], await pg.locator('#btnHeaderMenu').get_attribute('aria-expanded')))
                await pg.screenshot(path=f"{H.OUT}/tour-{mode}-{i}.png")
                nxt = pg.locator("#tutorialHost button", has_text="Next")
                if not await nxt.count():
                    nxt = pg.locator("#tutorialHost button").filter(has_text="Start")
                if not await nxt.count(): break
                await nxt.first.click(); await pg.wait_for_timeout(400)
            for s in seen: print(mode, s)
            await ctx.close()
    srv.terminate()
asyncio.run(main())
