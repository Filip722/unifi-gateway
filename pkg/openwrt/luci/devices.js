'use strict';
'require view';
'require dom';
'require poll';
'require rpc';
'require uci';
'require statistics.rrdtool as rrdtool';

const callHostHints = rpc.declare({
	object: 'luci-rpc', method: 'getHostHints', expect: { '': {} }
});

return view.extend({
	load() {
		return Promise.all([rrdtool.load(), L.resolveDefault(callHostHints(), {})]);
	},

	render(data) {
		const host = rrdtool.opts.host;
		const hints = data[1];
		const devices = rrdtool.pluginInstances(host, 'device_traffic').map(function(id) {
			const mac = id.match(/../g).join(':');
			const hint = hints[mac.toUpperCase()] || hints[mac] || {};
			const name = hint.name || mac;
			return { id: id, label: name + (hint.ipaddrs && hint.ipaddrs[0] ? ' (' + hint.ipaddrs[0] + ')' : '') };
		}).sort(function(a, b) { return a.label.localeCompare(b.label); });
		const heading = E('h2', {}, _('Device traffic'));
		if (!devices.length)
			return E('div', {}, [heading, E('p', {}, _('Waiting for the first device samples. Reload this page in a minute.'))]);

		const device = E('select', {}, devices.map(function(item) {
			return E('option', { value: item.id }, item.label);
		}));
		const timespan = E('select', {}, L.toArray(uci.get('luci_statistics', 'collectd_rrdtool', 'RRATimespans')).map(function(span) {
			return E('option', { value: span, selected: span === '2hour' ? true : null }, span);
		}));
		const graphs = E('div', { class: 'center' });
		const identity = E('p');
		const update = function() {
			const selected = device.value;
			const span = timespan.value;
			identity.textContent = selected.match(/../g).join(':');
			return rrdtool.render('device_traffic', selected, false, host, span).then(function(blobs) {
				if (device.value !== selected || timespan.value !== span)
					return;
				graphs.querySelectorAll('img').forEach(function(img) { URL.revokeObjectURL(img.src); });
				dom.content(graphs, blobs.map(function(blob) {
					return E('img', { src: URL.createObjectURL(new Blob([blob], { type: 'image/png' })), style: 'display:block;max-width:100%;margin:1em auto' });
				}));
			});
		};
		device.addEventListener('change', update);
		timespan.addEventListener('change', update);
		poll.add(update, 30);
		requestAnimationFrame(update);
		return E('div', {}, [
			heading,
			E('p', {}, _('Internet and routed LAN/VLAN traffic observed by OpenWrt. Traffic staying within a switch, access point or bridge is outside these counters. Gaps mean no measurement.')),
			E('p', { class: 'controls' }, [E('label', {}, [_('Device') + ' ', device]), ' ', E('label', {}, [_('Period') + ' ', timespan])]),
			identity, graphs
		]);
	},

	handleSave: null,
	handleSaveApply: null,
	handleReset: null
});
