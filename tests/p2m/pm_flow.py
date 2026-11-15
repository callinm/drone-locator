"""Mocked-API browser test of 'Make a map from my flight photos' at 412px and 1280px.
Checks: device-side shrink keeps GPS/XMP, batches, step states turn green with Success, map loads."""
import asyncio, glob, io, json, os, re, subprocess, sys, time
from playwright.async_api import async_playwright
from PIL import Image
sys.path.insert(0, os.path.dirname(__file__))
import local_harness as H

PHOTOS = sorted(glob.glob("/workspace/lostdrone/photos/*.JPG"))[:12]
FAIL = os.environ.get("PM_FAIL") == "1"

def parse_multipart(body: bytes, ctype: str):
    b = re.search(r"boundary=(.+)", ctype).group(1).encode()
    parts = body.split(b"--" + b)[1:-1]
    out = []
    for p in parts:
        head, _, data = p.partition(b"\r\n\r\n")
        out.append(data[:-2])
    return out

async def main():
    srv = subprocess.Popen([sys.executable, "-m", "http.server", "8765", "--bind", "127.0.0.1", "-d", H.ROOT], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(0.8)
    try:
        async with async_playwright() as p:
            b = await p.chromium.launch(executable_path=H.CHROME, args=["--no-sandbox"])
            for name, kw in (("phone412", H.PHONE), ("desk1280", H.DESK)):
                st = {"polls": 0, "photos": [], "job": None}
                def view(status, stage, pct, **extra):
                    j = {"id": "a" * 32, "status": status, "stage": stage, "progress_pct": pct, "photo_count": len(st["photos"]),
                         "gps_count": len(st["photos"]), "skipped": [], "mosaic_id": None, "error": None, "started_at": 1, "elapsed_s": 75 * st["polls"],
                         "photos_deleted": False, "can_retry": False}
                    j.update(extra); return j
                async def route(r):
                    url = r.request.url; path = url.split("8091", 1)[1].split("?")[0]; m = r.request.method
                    if path == "/auth/me":
                        return await r.fulfill(json=H.user("unlocked"))
                    if path == "/health":
                        return await r.fulfill(json={"ok": True})
                    if path == "/searchmaps/latest":
                        return await r.fulfill(json={"job": None})
                    if path == "/searchmaps" and m == "POST":
                        return await r.fulfill(status=201, json=view("collecting", "collecting", 0))
                    if path.endswith("/photos"):
                        parts = parse_multipart(r.request.post_data_buffer, r.request.headers["content-type"])
                        st["photos"] += parts
                        return await r.fulfill(json=view("collecting", "collecting", 0, added=len(parts)))
                    if path.endswith("/start"):
                        return await r.fulfill(json=view("submitting", "submitting", 1))
                    if path.startswith("/searchmaps/") and m == "GET":
                        st["polls"] += 1; n = st["polls"]
                        if n == 1: return await r.fulfill(json=view("queued", "queued", 5))
                        if n == 2: return await r.fulfill(json=view("processing", "matching_photos", 30))
                        if n == 3:
                            if FAIL:
                                return await r.fulfill(json=view("failed", "matching_photos", 30, error="The photos could not be matched to each other. Photos should point straight down and overlap by about two thirds.", can_retry=True))
                            return await r.fulfill(json=view("processing", "making_map", 75))
                        return await r.fulfill(json=view("done", "done", 100, mosaic_id="m" * 32, photos_deleted=True))
                    if path == "/mosaics/" + "m" * 32:
                        return await r.fulfill(json={"id": "m" * 32, "filename": "search-map.jpg", "width": 2000, "height": 1500, "georeferenced": True,
                                                     "bounds": {"west": -95.8341, "south": 44.4973, "east": -95.8289, "north": 44.5003}})
                    if path.endswith("/preview"):
                        return await r.fulfill(path=H.PHOTO, content_type="image/jpeg")
                    return await r.fulfill(status=404, json={"detail": "mock"})
                ctx = await b.new_context(**kw)
                await ctx.add_init_script("localStorage.setItem('dm_tutorial_completed','1')")
                await ctx.route("http://127.0.0.1:8091/**", route)
                pg = await ctx.new_page(); errs = []
                pg.on("pageerror", lambda e: errs.append(str(e)))
                # speed up polling
                await ctx.add_init_script("""(() => { const st = window.setTimeout; window.setTimeout = (f, d, ...a) => st(f, Math.min(d || 0, 300), ...a); })()""")
                await pg.goto("http://127.0.0.1:8765/index.html", wait_until="networkidle"); await pg.wait_for_timeout(600)
                await pg.screenshot(path=f"{H.OUT}/pm-{name}-0-start.png")
                await pg.set_input_files("#filePhotoMap", PHOTOS)
                shots = 0; seen = []
                t0 = time.time()
                while time.time() - t0 < 60:
                    txt = await pg.locator("#photoMapStatus").inner_text()
                    cls = await pg.evaluate("[...document.querySelectorAll('[data-pm-step]')].map(l => l.dataset.pmStep + ':' + (l.className||'-')).join(' ')")
                    if not seen or seen[-1][1] != cls:
                        seen.append((txt[:90], cls))
                        await pg.screenshot(path=f"{H.OUT}/pm-{name}-{len(seen)}.png"); shots += 1
                    if "Success" in txt or "match" in txt and "failed" in cls:
                        break
                    await pg.wait_for_timeout(100)
                for s in seen: print(name, s)
                await pg.wait_for_timeout(500)
                await pg.screenshot(path=f"{H.OUT}/pm-{name}-final.png")
                print(name, "progress:", await pg.locator("#findProgress").inner_text())
                print(name, "retry visible:", await pg.locator("#btnPhotoMapRetry").is_visible(), "map:", await pg.evaluate("(() => { const b = document.querySelector('.find-map-wrap').getBoundingClientRect(); return [Math.round(b.top), Math.round(b.height)]; })()"))
                sizes = []
                for data in st["photos"]:
                    im = Image.open(io.BytesIO(data)); ex = im.getexif().get_ifd(0x8825)
                    sizes.append((im.size, bool(ex), b"RelativeAltitude" in data[:200000], len(data)//1024))
                print(name, "uploaded", len(st["photos"]), "first:", sizes[:2], "all<=2048:", all(max(s[0]) <= 2048 for s in sizes), "all GPS:", all(s[1] for s in sizes), "all XMP:", all(s[2] for s in sizes))
                print(name, "errors:", errs[:3])
                await ctx.close()
            await b.close()
    finally:
        srv.terminate()

asyncio.run(main())
