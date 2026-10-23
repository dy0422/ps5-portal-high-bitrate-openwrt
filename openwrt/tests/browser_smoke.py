"""Real LuCI browser test; run with luci_fixture.py in a disposable container."""
import json
import os
from pathlib import Path
from playwright.sync_api import sync_playwright


def main():
    if os.environ.get('PORTAL_TEST_DISPOSABLE') != '1':
        raise SystemExit('Disposable test container only')
    base = 'http://127.0.0.1:18080'
    out = Path(__file__).resolve().parents[2] / 'release-assets'
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path='/usr/bin/chromium', args=['--no-sandbox'])
        page = browser.new_page(viewport={'width': 1280, 'height': 1050})
        errors = []
        page.on('pageerror', lambda error: errors.append(str(error)))
        page.goto(base + '/cgi-bin/luci/admin/services/portal-bitrate')
        page.locator('input[name="luci_username"]').fill('root')
        page.locator('input[name="luci_password"]').fill('portal-luci-test')
        page.get_by_role('button', name='Log in', exact=True).click()
        page.get_by_role('heading', name='PS Portal 提码率', exact=True).wait_for()

        def click(name):
            page.get_by_role('button', name=name, exact=True).click()

        def log_contains(text, timeout=40000):
            page.wait_for_function('''(text) => {
                var value = document.querySelector("pre").textContent;
                return text === 'BASELINE READY' || text === 'AUTO READY'
                    ? value.split('\\n').some(line => line.startsWith(text)) : value.includes(text);
            }''', arg=text, timeout=timeout)

        def message_contains(text, timeout=40000):
            page.wait_for_function('(text) => document.querySelector("[role=status]").textContent.includes(text)', arg=text, timeout=timeout)

        if not os.environ.get('PORTAL_TEST_RESUME'):
            page.locator('#pb-address').fill('10.77.0.10/24')
            page.locator('#pb-ps5_ip').fill('10.77.0.10')
            page.locator('#pb-portal_ip').fill('10.77.0.20')
            click('保存并检测设备')
            page.get_by_text('Use three distinct addresses in a /30 or larger IPv4 LAN', exact=True).wait_for()
            page.locator('#pb-address').fill('10.77.0.250/24')
            click('保存并检测设备')
            message_contains('设备检测完成')
            print('PASS: authenticated form validation and enrollment through rpcd', flush=True)

            click('连接测试（60 秒）')
            log_contains('BASELINE READY')
            click('停止')
            log_contains('已停止，网络恢复完成')
            assert page.get_by_role('button', name='试运行（90 秒）', exact=True).is_disabled()
            print('PASS: cancel baseline from browser; no success receipt retained', flush=True)

            click('连接测试（60 秒）')
            log_contains('BASELINE READY')
            message_contains('连接测试完成', timeout=90000)
            print('PASS: full 60-second baseline with live browser polling', flush=True)
            click('试运行（90 秒）')
            log_contains('AUTO READY')
            page.wait_for_function('document.querySelectorAll("table tr")[2].textContent.includes("1")')
            click('停止')
            log_contains('已停止，网络恢复完成')
            print('PASS: bounded trial, modified handshake and graceful browser cancellation', flush=True)

        if os.environ.get('PORTAL_TEST_RESUME') != 'verify':
            page.locator('#pb-autostart').check()
            click('启动服务')
            message_contains('服务已启动')
            page.screenshot(path=str(out / 'luci-preview.png'), full_page=True)
            assert page.locator('#pb-address').is_disabled()
            click('停止')
            message_contains('服务已停止')
            click('环境检查')
            log_contains('BPF compile: OK')
            print('PASS: persistent service/autostart, stop and diagnostics from real LuCI', flush=True)

        anonymous = p.request.new_context()
        response = anonymous.post(base + '/cgi-bin/luci/admin/ubus', data=json.dumps({
            'jsonrpc': '2.0', 'id': 9, 'method': 'call',
            'params': ['0' * 32, 'portal-bitrate', 'start', {'autostart': 0}]
        }), headers={'Content-Type': 'application/json'})
        assert response.json().get('error', {}).get('code') == -32002, response.text()
        anonymous.dispose()
        print('PASS: unauthenticated mutation denied by ubus ACL', flush=True)

        page.set_viewport_size({'width': 390, 'height': 844})
        page.reload()
        page.get_by_role('heading', name='PS Portal 提码率', exact=True).wait_for()
        assert page.evaluate('document.documentElement.scrollWidth <= window.innerWidth + 1')
        page.screenshot(path=str(out / 'luci-mobile-preview.png'), full_page=True)
        assert not errors, errors
        print('PASS: mobile layout and no browser JavaScript errors', flush=True)
        browser.close()


if __name__ == '__main__':
    main()
