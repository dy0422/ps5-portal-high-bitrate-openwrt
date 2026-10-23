'use strict';
'require view';
'require rpc';
'require poll';
'require ui';

var callStatus = rpc.declare({ object: 'portal-bitrate', method: 'status', expect: {} });
var callConfigure = rpc.declare({ object: 'portal-bitrate', method: 'configure',
	params: [ 'bridge', 'address', 'ps5_ip', 'portal_ip', 'profile' ], expect: {} });
var callStart = rpc.declare({ object: 'portal-bitrate', method: 'start', params: [ 'autostart' ], expect: {} });
var calls = {};
[ 'baseline', 'trial', 'stop', 'repair', 'doctor' ].forEach(function(name) {
	calls[name] = rpc.declare({ object: 'portal-bitrate', method: name, expect: {} });
});

var actionNames = { configure: '设备检测', baseline: '连接测试', trial: '试运行',
	start: '启动服务', stop: '停止服务', repair: '网络恢复', doctor: '环境检查' };

return view.extend({
	load: function() { return callStatus(); },

	field: function(name, label, input, description) {
		input.id = 'pb-' + name;
		this.fields[name] = input;
		return E('div', { 'class': 'cbi-value' }, [
			E('label', { 'class': 'cbi-value-title', 'for': input.id }, label),
			E('div', { 'class': 'cbi-value-field' }, [ input,
				E('div', { 'class': 'cbi-value-description' }, description || '') ])
		]);
	},

	button: function(name, label, primary) {
		var b = E('button', { 'type': 'button',
			'class': 'cbi-button ' + (primary ? 'cbi-button-apply' : 'cbi-button-action'),
			'click': ui.createHandlerFn(this, this.run, name) }, label);
		this.buttons[name] = b;
		return b;
	},

	run: function(name) {
		var self = this, request;
		if (name === 'configure') {
			var values = [ 'bridge', 'address', 'ps5_ip', 'portal_ip', 'profile' ].map(function(k) {
				return self.fields[k].value.trim();
			});
			if (values.some(function(v) { return !v; })) {
				ui.addNotification(null, E('p', '请填写全部地址，并选择网络和码率。'), 'warning');
				return Promise.resolve();
			}
			request = function() { return callConfigure.apply(null, values); };
		}
		else if (name === 'start')
			request = function() { return callStart(self.autostart.checked ? 1 : 0); };
		else
			request = calls[name];
		this.pending = true;
		this.update(this.latest);
		return request().then(function(result) {
			if (!result.ok) throw new Error(result.error || '操作未成功。');
			return callStatus();
		}).then(function(data) { self.update(data); }).catch(function(error) {
			ui.addNotification(null, E('p', error.message || String(error)), 'danger');
		}).finally(function() {
			self.pending = false;
			self.update(self.latest);
		});
	},

	update: function(data) {
		if (!data || !data.ok) return;
		this.latest = data;
		var job = data.job || {}, busy = !!job.busy || this.pending, locked = this.readonly || busy;
		Object.keys(this.fields).forEach(function(k) {
			this.fields[k].disabled = locked || data.running;
		}, this);
		this.buttons.configure.disabled = locked || data.running;
		this.buttons.baseline.disabled = locked || data.running || !data.configured;
		this.buttons.trial.disabled = locked || data.running || !data.baseline_saved;
		this.buttons.start.disabled = locked || data.running || !data.baseline_saved;
		this.buttons.doctor.disabled = locked;
		this.buttons.repair.disabled = locked || data.running;
		this.buttons.stop.disabled = this.readonly || this.pending ||
			!(job.busy || data.running || data.recovery_required || job.state === 'failed');
		this.autostart.disabled = locked || data.running;

		var state = data.running ? (data.mode === 'baseline' ? '连接测试中' : '中继运行中') : '服务已停止';
		if (job.busy) state = (actionNames[job.action] || '操作') + '进行中';
		if (job.stopping) state = '正在停止并恢复网络';
		if (!job.busy && data.recovery_required) state = '需要恢复网络';
		this.stateNode.textContent = state;
		this.baselineNode.textContent = data.baseline_saved ? '已保存（启动时校验）' : '尚未完成';
		this.countNode.textContent = String(data.modified || 0);
		this.bootNode.textContent = data.autostart ? '已开启' : '已关闭';

		var message = '先保存并检测设备，再完成连接测试和试运行。';
		if (job.busy && job.action === 'configure') message = '请保持 PS5 和 Portal 开机，正在检测设备。';
		if (job.busy && job.action === 'baseline') message = '等日志出现 BASELINE READY 后连接 Portal，检查约 60 秒的画面和按键。';
		if (job.busy && job.action === 'trial') message = '等日志出现 AUTO READY 后重新连接 Portal；90 秒后自动停止，请检查实际画面与码率。';
		if (job.busy && job.action === 'stop') message = '正在等待服务退出并恢复网络。';
		if (job.state === 'complete' && job.action === 'configure') message = '设备检测完成。请先断开串流，再开始 60 秒连接测试。';
		if (job.state === 'complete' && job.action === 'baseline') message = '连接测试完成。确认画面和按键正常后，开始试运行。';
		if (job.state === 'complete' && job.action === 'trial') message = '试运行已结束。确认效果正常后，可启动服务持续使用。';
		if (job.state === 'complete' && job.action === 'start') message = '服务已启动，每次新的 Portal 连接会自动应用设置。请在设备上确认实际画面与码率。';
		if (job.state === 'complete' && job.action === 'stop') message = '服务已停止，网络恢复完成。';
		if (job.state === 'cancelled') message = '操作已停止，网络恢复完成。';
		if (job.state === 'failed') message = job.error || '操作失败，请查看日志。';
		if (job.stopping) message = '正在等待恢复完成，请保持路由器运行。';
		this.messageNode.textContent = message;
		var bottom = this.logNode.scrollHeight - this.logNode.scrollTop - this.logNode.clientHeight < 40;
		this.logNode.textContent = data.log || '操作日志会显示在这里。';
		if (bottom) this.logNode.scrollTop = this.logNode.scrollHeight;
	},

	render: function(data) {
		var self = this, cfg = data.config || {};
		this.fields = {};
		this.buttons = {};
		this.readonly = !L.hasViewPermission();
		this.stateNode = E('strong', {}, '正在读取');
		this.baselineNode = E('span');
		this.countNode = E('span');
		this.bootNode = E('span');
		this.messageNode = E('p', { 'role': 'status', 'aria-live': 'polite' });
		this.logNode = E('pre', { 'style': 'max-height:300px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere',
			'aria-label': '操作日志' });
		var bridgeNames = (data.bridges || []).slice();
		var selectedBridge = cfg.bridge || 'br-lan';
		if (bridgeNames.indexOf(selectedBridge) < 0) bridgeNames.unshift(selectedBridge);
		var bridge = E('select', { 'class': 'cbi-input-select' }, bridgeNames.map(function(name) {
			return E('option', { 'value': name }, name + ((data.bridges || []).indexOf(name) < 0 ? '（当前不可用）' : ''));
		}));
		bridge.value = selectedBridge;
		var profile = E('select', { 'class': 'cbi-input-select' }, [
			E('option', { 'value': '65' }, '65 Mbps（建议起点）'),
			E('option', { 'value': '100' }, '100 Mbps'),
			E('option', { 'value': '200' }, '200 Mbps（实验）') ]);
		profile.value = cfg.profile || '65';
		function input(value, placeholder) {
			return E('input', { 'type': 'text', 'class': 'cbi-input-text', 'value': value || '',
				'placeholder': placeholder, 'maxlength': 80, 'spellcheck': 'false', 'autocomplete': 'off' });
		}
		this.autostart = E('input', { 'type': 'checkbox', 'id': 'pb-autostart' });
		this.autostart.checked = !!data.autostart;
		var page = E('div', { 'class': 'cbi-map' }, [
			E('h2', {}, 'PS Portal 提码率'),
			E('div', { 'class': 'cbi-map-descr' }, '为同一局域网内的 PS5 与 Portal 请求更高的串流码率。实验功能，目标码率不代表实际码率。'),
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, '运行状态'),
				E('table', { 'class': 'table' }, [
					E('tr', { 'class': 'tr' }, [ E('td', { 'class': 'td' }, '服务'), E('td', { 'class': 'td' }, this.stateNode) ]),
					E('tr', { 'class': 'tr' }, [ E('td', { 'class': 'td' }, '连接测试记录'), E('td', { 'class': 'td' }, this.baselineNode) ]),
					E('tr', { 'class': 'tr' }, [ E('td', { 'class': 'td' }, '本次已修改的连接请求'), E('td', { 'class': 'td' }, this.countNode) ]),
					E('tr', { 'class': 'tr' }, [ E('td', { 'class': 'td' }, '开机启动'), E('td', { 'class': 'td' }, this.bootNode) ])
				]), this.messageNode
			]),
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, '1. 设置并检测设备'),
				this.field('bridge', '局域网网桥', bridge, '选择 PS5 和 Portal 所在的 LAN，通常是 br-lan。'),
				this.field('address', '中继独立地址', input(cfg.address, '例如 192.168.1.250/24'), '填写一个空闲 LAN 地址及前缀，避开路由器地址和 DHCP 动态地址池。'),
				this.field('ps5_ip', 'PS5 地址', input(cfg.ps5_ip, '例如 192.168.1.100'), '请为两台设备设置 DHCP 静态租约，并保持开机。'),
				this.field('portal_ip', 'Portal 地址', input(cfg.portal_ip, '例如 192.168.1.101'), ''),
				this.field('profile', '目标码率', profile, '先尝试 65 Mbps，稳定后再比较 100 Mbps。修改设置前请停止服务。'),
				E('p', {}, this.button('configure', '保存并检测设备', true))
			]),
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, '2. 测试并启动'),
				E('p', {}, '先做不修改码率的连接测试，确认画面和按键正常，再试运行。每次测试开始前请先断开 Portal 串流。'),
				E('p', { 'style': 'display:flex;gap:.6em;flex-wrap:wrap' }, [
					this.button('baseline', '连接测试（60 秒）'), this.button('trial', '试运行（90 秒）') ]),
				E('p', {}, [ this.autostart, ' ', E('label', { 'for': 'pb-autostart' }, '随路由器开机启动'),
					E('span', { 'class': 'cbi-value-description' }, '　点击“启动服务”时保存。') ]),
				E('p', { 'style': 'display:flex;gap:.6em;flex-wrap:wrap' }, [
					this.button('start', '启动服务', true), this.button('stop', '停止') ])
			]),
			E('div', { 'class': 'cbi-section' }, [
				E('h3', {}, '日志与维护'),
				E('p', { 'style': 'display:flex;gap:.6em;flex-wrap:wrap' }, [
					this.button('doctor', '环境检查'), this.button('repair', '恢复网络') ]),
				this.logNode,
				E('p', { 'class': 'cbi-value-description' }, '恢复未完成时请先点击停止，等待结束后再恢复网络。日志中的局域网地址请勿公开分享。')
			])
		]);
		this.update(data);
		poll.add(function() {
			return callStatus().then(function(value) {
				if (!value.ok) throw new Error(value.error || '无法读取服务状态');
				self.update(value);
			}).catch(function() {
				self.stateNode.textContent = '状态暂时不可用';
				self.messageNode.textContent = '无法读取服务状态，请检查路由器连接后刷新页面。';
				Object.keys(self.buttons).forEach(function(k) { self.buttons[k].disabled = true; });
			});
		}, 3);
		return page;
	},

	handleSaveApply: null,
	handleSave: null,
	handleReset: null
});
