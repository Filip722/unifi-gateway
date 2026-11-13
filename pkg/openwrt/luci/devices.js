'use strict';
'require view';
'require fs';
'require poll';
'require rpc';
'require statistics.rrdtool as rrdtool';

const callHostHints = rpc.declare({
	object: 'luci-rpc', method: 'getHostHints', expect: { '': {} }
});
const periods = [
	[7200, _('Last 2 hours')], [86400, _('Last 24 hours')],
	[604800, _('Last 7 days')], [2678400, _('Last month')], [31622400, _('Last year')]
];

return view.extend({
	load() {
		for (const file of ['uplot/uPlot.min.css', 'devices.css']) {
			const url = L.resource('statistics/' + file);
			if (!document.querySelector('link[href="' + url + '"]'))
				document.head.appendChild(E('link', { rel: 'stylesheet', href: url }));
		}
		const library = window.uPlot ? Promise.resolve() : new Promise(function(resolve, reject) {
			document.head.appendChild(E('script', {
				src: L.resource('statistics/uplot/uPlot.iife.min.js'),
				load: resolve, error: function() { reject(new Error(_('Unable to load charts'))); }
			}));
		});
		return Promise.all([rrdtool.load(), L.resolveDefault(callHostHints(), {}), library]);
	},

	parseSeries(text, start, end) {
		const points = [];
		for (const line of text.split('\n')) {
			const match = line.match(/^\s*(\d+):\s+(\S+)/);
			if (!match || +match[1] < start || +match[1] > end) continue;
			const value = Number(match[2]);
			points.push([+match[1], Number.isFinite(value) && value >= 0 ? value * 8 : null]);
		}
		return points;
	},

	formatRate(value) {
		if (value == null || !Number.isFinite(value)) return '—';
		const units = ['b/s', 'kb/s', 'Mb/s', 'Gb/s'];
		const unit = Math.min(3, Math.max(0, Math.floor(Math.log10(value || 1) / 3)));
		return (value / Math.pow(1000, unit)).toLocaleString(undefined, { maximumFractionDigits: 2 }) + ' ' + units[unit];
	},

	render(data) {
		const host = rrdtool.opts.host;
		const hints = data[1];
		const devices = rrdtool.pluginInstances(host, 'device_traffic').map(function(id) {
			const mac = id.match(/../g).join(':');
			const hint = hints[mac.toUpperCase()] || hints[mac] || {};
			return { id: id, mac: mac, name: hint.name || mac, ip: (hint.ipaddrs || [])[0] || '' };
		}).sort(function(a, b) { return a.name.localeCompare(b.name); });
		if (!devices.length) return E('p', {}, _('Waiting for device history. Reload this page in a minute.'));
		const device = E('select', { 'aria-label': _('Device') }, devices.map(function(item) {
			return E('option', { value: item.id }, item.name + (item.ip ? ' · ' + item.ip : ''));
		}));
		const period = E('select', { 'aria-label': _('Period') }, periods.map(function(item) {
			return E('option', { value: item[0] }, item[1]);
		}));
		const identity = E('span', { class: 'traffic-identity' });
		const status = E('span', { class: 'traffic-status', role: 'status' }, _('Loading history…'));
		const wrapper = E('div', { class: 'traffic-dashboard' });
		const formatRate = this.formatRate;
		let bounds = [];
		let request = 0;
		const cards = ['internet', 'local'].map(function(source) {
			const download = E('strong', { class: 'traffic-download' }, '—');
			const upload = E('strong', { class: 'traffic-upload' }, '—');
			const stamp = E('span', {}, _('No recent measurement'));
			const plot = E('div', { class: 'traffic-plot' });
			const empty = E('div', { class: 'traffic-empty' }, _('No measured samples in this period'));
			const card = E('section', { class: 'traffic-card' }, [
				E('h3', {}, source === 'internet' ? _('Internet') : _('LAN / VLAN')),
				E('p', { class: 'traffic-description' }, source === 'internet' ? _('Traffic to and from the Internet') : _('Routed traffic between local networks')),
				E('div', { class: 'traffic-metrics' }, [
					E('div', {}, [E('span', {}, _('↓ Download')), download]),
					E('div', {}, [E('span', {}, _('↑ Upload')), upload])
				]),
				E('div', { class: 'traffic-plot-wrap' }, [plot, empty]),
				E('p', { class: 'traffic-sample' }, stamp)
			]);
			return { source: source, node: card, plot: plot, empty: empty, download: download, upload: upload, stamp: stamp, chart: null };
		});
		const showValues = function(card, chart, index) {
			const hovering = index != null;
			if (!hovering) {
				index = chart.data[0].length - 1;
				while (index >= 0 && chart.data[1][index] == null && chart.data[2][index] == null) index--;
				const interval = chart.data[0][1] - chart.data[0][0] || 30;
				if (index >= 0 && bounds[1] - chart.data[0][index] > Math.max(90, interval * 2)) index = -1;
			}
			card.download.textContent = formatRate(chart.data[1][index]);
			card.upload.textContent = formatRate(chart.data[2][index]);
			card.stamp.textContent = index < 0 ? _('No recent measurement') :
				(hovering ? _('Sample at ') : _('Latest sample · ')) + new Date(chart.data[0][index] * 1000).toLocaleString();
		};
		const update = () => {
			const selected = device.value, span = +period.value, sequence = ++request;
			const end = Math.floor(Date.now() / 1000) - 15, start = end - span;
			const resolution = Math.max(30, Math.ceil(span / 600 / 30) * 30);
			const item = devices.find(function(d) { return d.id === selected; });
			identity.textContent = item.name + ' · ' + item.mac;
			return Promise.all(cards.map(card => Promise.all(['download', 'upload'].map(direction => {
				const path = rrdtool.opts.rrdpath + '/' + host + '/device_traffic-' + selected + '/bytes-' + card.source + '_' + direction + '.rrd';
				return fs.exec_direct('/usr/bin/rrdtool', ['fetch', path, 'AVERAGE', '-s', start, '-e', end, '-r', resolution])
					.then(text => this.parseSeries(text, start, end));
			})))).then(results => {
				if (sequence !== request) return;
				bounds = [start, end];
				cards.forEach(function(card, index) {
					const rows = new Map();
					results[index].forEach(function(points, direction) {
						points.forEach(function(point) {
							if (!rows.has(point[0])) rows.set(point[0], [null, null]);
							rows.get(point[0])[direction] = point[1];
						});
					});
					const times = Array.from(rows.keys()).sort(function(a, b) { return a - b; });
					const series = [times, times.map(t => rows.get(t)[0]), times.map(t => rows.get(t)[1])];
					card.empty.hidden = series[1].concat(series[2]).some(v => v != null);
					if (card.chart) card.chart.setData(series);
					else {
						const color = getComputedStyle(wrapper).color;
						const grid = getComputedStyle(wrapper).getPropertyValue('--traffic-grid').trim();
						card.chart = new window.uPlot({
							width: Math.max(260, card.plot.clientWidth), height: 260,
							legend: { show: false },
							cursor: { drag: { setScale: false }, sync: { key: 'device-traffic' } },
							scales: { x: { time: true, range: () => bounds }, y: { range: (u, min, max) => [0, max > 0 ? max * 1.15 : 1] } },
							axes: [
								{ stroke: color, grid: { stroke: grid, width: 1 }, ticks: { show: false }, font: '12px sans-serif', space: 85 },
								{ stroke: color, grid: { stroke: grid, width: 1 }, ticks: { show: false }, font: '12px sans-serif', size: 82, values: (u, values) => values.map(formatRate) }
							],
							series: [{},
								{ label: _('Download'), stroke: '#3b82f6', fill: '#3b82f61a', width: 2, spanGaps: false },
								{ label: _('Upload'), stroke: '#a855f7', width: 2, spanGaps: false }
							],
							hooks: { setCursor: [u => showValues(card, u, u.cursor.idx)] }
						}, series, card.plot);
					}
					showValues(card, card.chart, card.chart.cursor.idx);
				});
				status.textContent = _('Updated ') + new Date().toLocaleTimeString();
			}).catch(function(error) {
				if (sequence === request) status.textContent = _('Unable to load history: ') + error.message;
			});
		};
		device.addEventListener('change', update);
		period.addEventListener('change', update);
		const resize = new ResizeObserver(function() {
			cards.forEach(function(card) {
				if (card.chart) card.chart.setSize({ width: Math.max(260, card.plot.clientWidth), height: 260 });
			});
		});
		cards.forEach(function(card) { resize.observe(card.plot); });
		poll.add(update, 30);
		requestAnimationFrame(update);
		wrapper.appendChild(E('div', { class: 'traffic-heading' }, [
			E('div', {}, [E('h2', {}, _('Device traffic')), E('p', {}, _('Per-device Internet and local traffic history'))]),
			E('span', { class: 'traffic-refresh' }, _('Refreshes every 30s'))
		]));
		wrapper.appendChild(E('div', { class: 'traffic-toolbar' }, [
			E('label', {}, [E('span', {}, _('Device')), device]),
			E('label', {}, [E('span', {}, _('Period')), period])
		]));
		wrapper.appendChild(E('div', { class: 'traffic-meta' }, [identity, status]));
		wrapper.appendChild(E('div', { class: 'traffic-cards' }, cards.map(c => c.node)));
		wrapper.appendChild(E('p', { class: 'traffic-footnote' }, _('Hover a chart to inspect a sample. Gaps mean no measurement. Traffic staying within a switch, access point or bridge is outside these counters.')));
		return wrapper;
	},

	handleSave: null,
	handleSaveApply: null,
	handleReset: null
});
