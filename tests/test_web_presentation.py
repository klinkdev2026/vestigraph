"""Screenshot-first preview, real Chromium UI, synthetic data only."""
import base64
import time
import pytest
from playwright.sync_api import expect

from tests.fixtures_web_browser import browser, stack, sign_in
from tests.fixtures_presentation import png
from vestigraph.presentation import Presentation


def test_screenshot_navigation_rename_and_csp(browser, stack):
    app = stack["app"]
    endpoint = stack["endpoint"]
    original = endpoint.call
    def call(method, params=None, timeout=None):
        if method == "view.screenshot":
            return {"data_url": "data:image/png;base64," + base64.b64encode(png()).decode()}
        return original(method, params, timeout)
    endpoint.call = call
    doc = next(d for d in app.documents(stack["project"]["id"]) if not d["read_only"])
    endpoint.state = 1
    # Named save waits for durable capture and screenshot attempt.
    job = app.milestone(doc["id"], "Screenshot version")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = app.catalog.get_job(job["id"])
        if status["status"] in ("succeeded", "failed"):
            break
        time.sleep(.02)
    assert status["status"] == "succeeded", status
    cp = app.checkpoints(doc["id"])["items"][0]
    assert app.thumbnail(doc["id"], cp["id"]) is not None
    context, page = sign_in(browser, stack)
    # Test production CSP, not the legacy fixture's bypass.
    context.close()
    context = browser.new_context(viewport={"width":1280,"height":1000}, locale="zh-CN")
    page = context.new_page()
    errors=[]
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    token=stack["auth"].issue_bootstrap()
    page.goto(stack["base"]+"/#bootstrap="+token)
    page.locator("#main").wait_for(state="visible")
    page.goto(stack["base"]+"/#doc="+doc["id"]+"&version="+cp["id"])
    page.locator(f"#checkpoints .item[data-id='{cp['id']}']").click()
    expect(page.locator("#preview-status")).to_have_text("保存时的画面", timeout=15000)
    assert page.locator("#canvas").get_attribute("data-items") == "1"
    page.locator("#zoom-in").click()
    assert float(page.locator("#canvas").get_attribute("data-zoom")) > .9
    box = page.locator("#canvas").bounding_box()
    page.mouse.move(box["x"]+box["width"]/2, box["y"]+box["height"]/2)
    page.mouse.down()
    page.mouse.move(box["x"]+box["width"]/2+35, box["y"]+box["height"]/2+10, steps=4)
    page.mouse.up()
    assert page.locator("#canvas").get_attribute("data-panned") == "true"
    page.locator("#fit").click()
    page.locator("#version-name").fill("编号方向已纠正")
    page.locator("#rename-version").click()
    expect(page.locator("#facts")).to_contain_text("编号方向已纠正")
    assert app.checkpoint(doc["id"], cp["id"])["title"] == "编号方向已纠正"
    page.reload()
    expect(page.locator("#facts")).to_contain_text("编号方向已纠正", timeout=15000)
    assert not errors, errors
    context.close()


def test_large_legacy_generates_saved_layout_image(browser, stack, tmp_path):
    import klayout.db as db
    from vestigraph.store import Repository
    layout = db.Layout()
    top = layout.create_cell("TOP")
    shapes = top.shapes(layout.layer(1, 0))
    for i in range(20000):
        x, y = (i % 200) * 20, (i // 200) * 20
        shapes.insert(db.Box(x, y, x+10, y+10))
    source = tmp_path / "legacy-large.gds"
    layout.write(str(source))
    assert source.stat().st_size > 1024*1024
    repo = Repository.init(tmp_path / "legacy-large-history")
    cp = repo.checkpoint(source)
    doc = stack["app"].catalog.add_history(stack["project"]["id"], repo.root, read_only=True)
    context, page = sign_in(browser, stack)
    requests = []
    page.on("request", lambda r: requests.append(r.url) if r.method == "POST" else None)
    try:
        page.goto(stack["base"]+"/#doc="+doc["id"])
        page.locator("#checkpoints .item").first.click()
        expect(page.locator("#preview-status")).to_have_text("\u4fdd\u5b58\u7248\u56fe\u9884\u89c8", timeout=30000)
        assert page.locator("#canvas").get_attribute("data-items") == "1"
        assert any(url.endswith("/thumbnail") for url in requests)
        assert not any(url.endswith("/preview") for url in requests)
        assert not (repo.root / "presentation.sqlite3").exists()
        assert stack["app"].thumbnail(doc["id"],cp["id"]) is not None
        assert not page.errors
    finally:
        context.close()

@pytest.mark.parametrize("reason,zh,en", [
    ("timeout", "截图等待超时", "Taking the screenshot timed out"),
    ("unsupported", "当前编辑器连接不提供截图能力", "does not provide screenshots"),
    ("invalid_image", "编辑器返回的图片无效", "returned an invalid image"),
])
def test_missing_image_reason_bilingual_without_offline_renderer(browser, stack, monkeypatch, reason, zh, en):
    app = stack["app"]
    capabilities = app.capabilities
    monkeypatch.setattr(app, "capabilities", lambda: {**capabilities(), "generated_thumbnails": False})
    doc = next(d for d in app.documents(stack["project"]["id"]) if not d["read_only"])
    cp = app.checkpoints(doc["id"])["items"][0]
    capture_id = app.checkpoint(doc["id"], cp["id"])["metadata"]["capture_id"]
    p = Presentation(app.catalog.get_document(doc["id"])["store_path"])
    p.discard_thumbnail(capture_id)
    p.record_thumbnail_failure(capture_id, reason)
    original = app.checkpoints
    def large(did, *args, **kwargs):
        result = original(did, *args, **kwargs)
        if did == doc["id"]:
            for item in result["items"]:
                item["size"] = 256 * 1024 * 1024
        return result
    monkeypatch.setattr(app, "checkpoints", large)
    context, page = sign_in(browser, stack)
    try:
        page.goto(stack["base"]+"#doc="+doc["id"]+"&version="+cp["id"])
        page.locator(f"#checkpoints .item[data-id='{cp['id']}']").click()
        expect(page.locator("#preview-note")).to_contain_text(zh, timeout=15000)
        page.locator("button[data-lang='en']").click()
        expect(page.locator("#preview-note")).to_contain_text(en)
        assert not page.errors
    finally:
        context.close()
