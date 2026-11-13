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
			if (!match) continue;
			const value = Number(match[2]);
			points.push([+match[1], Number.isFinite(value) && value >= 0 ? value * 8 : null]);
		}
		// RRD timestamps mark the end of each averaged interval.
		const interval = points.length > 1 ? points[1][0] - points[0][0] : 0;
		let bytes = 0, seconds = 0;
		for (const [time, value] of points) {
			const overlap = Math.max(0, Math.min(end, time) - Math.max(start, time - interval));
			if (value != null && overlap > 0) {
				bytes += value / 8 * overlap;
				seconds += overlap;
			}
		}
		return { points: points.filter(p => p[0] >= start && p[0] <= end), bytes: seconds ? bytes : null, seconds: seconds };
	},

	historyWindows(start, end) {
		const windows = [];
		let right = end;
		for (const [span] of periods) {
			const left = Math.max(start, end - span);
			if (left < right) windows.push([left, right]);
			right = left;
			if (left === start) break;
		}
		return windows.reverse();
	},

	formatBytes(value) {
		if (value == null) return '—';
		const unit = Math.min(4, Math.max(0, Math.floor(Math.log10(value || 1) / 3)));
		return (value / Math.pow(1000, unit)).toLocaleString(undefined, { maximumFractionDigits: 2 }) + ' ' + ['B', 'kB', 'MB', 'GB', 'TB'][unit];
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
			const totals = ['download', 'upload'].map(function(direction) {
				return { value: E('strong', { class: 'traffic-' + direction }, '—'), coverage: E('small', {}, _('No stored intervals')) };
			});
			const stamp = E('span', {}, _('No recent measurement'));
			const plot = E('div', { class: 'traffic-plot' });
			const empty = E('div', { class: 'traffic-empty' }, _('No measured samples in this period'));
			const card = E('section', { class: 'traffic-card' }, [
				E('h3', {}, source === 'internet' ? _('Internet') : _('LAN / VLAN')),
				E('p', { class: 'traffic-description' }, source === 'internet' ? _('Traffic to and from the Internet') : _('Routed traffic between local networks')),
				E('div', { class: 'traffic-metrics traffic-totals' }, totals.map(function(total, index) {
					return E('div', {}, [E('span', {}, index ? _('↑ Uploaded in period · estimated') : _('↓ Downloaded in period · estimated')), total.value, total.coverage]);
				})),
				E('div', { class: 'traffic-metrics' }, [
					E('div', {}, [E('span', {}, _('↓ Download rate')), download]),
					E('div', {}, [E('span', {}, _('↑ Upload rate')), upload])
				]),
				E('div', { class: 'traffic-plot-wrap' }, [plot, empty]),
				E('p', { class: 'traffic-sample' }, stamp)
			]);
			return { source: source, node: card, plot: plot, empty: empty, totals: totals, download: download, upload: upload, stamp: stamp, chart: null };
		});
		const showValues = function(card, chart, index) {
			const hovering = index != null;
			if (!hovering) {
				index = chart.data[0].length - 1;
				while (index >= 0 && chart.data[1][index] == null && chart.data[2][index] == null) index--;
				const interval = chart.data[0][index] - chart.data[0][index - 1] || 30;
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
			const windows = this.historyWindows(start, end);
			const item = devices.find(function(d) { return d.id === selected; });
			identity.textContent = item.name + ' · ' + item.mac;
			status.textContent = _('Loading history…');
			cards.forEach(card => card.totals.forEach(function(total) {
				total.value.textContent = '—';
				total.coverage.textContent = _('Loading…');
			}));
			return Promise.all(cards.map(card => Promise.all(['download', 'upload'].map(direction => {
				const path = rrdtool.opts.rrdpath + '/' + host + '/device_traffic-' + selected + '/bytes-' + card.source + '_' + direction + '.rrd';
				return Promise.all(windows.map(([left, right]) =>
					fs.exec_direct('/usr/bin/rrdtool', ['fetch', path, 'AVERAGE', '-s', left, '-e', right, '-r', 30])
						.then(text => this.parseSeries(text, left, right))));
			})))).then(results => {
				if (sequence !== request) return;
				bounds = [start, end];
				cards.forEach((card, index) => {
					const rows = new Map();
					results[index].forEach((bands, direction) => {
						const seconds = bands.reduce((sum, band) => sum + band.seconds, 0);
						const percent = seconds / span * 100;
						card.totals[direction].value.textContent = this.formatBytes(seconds ? bands.reduce((sum, band) => sum + (band.bytes || 0), 0) : null);
						card.totals[direction].coverage.textContent = seconds ? _('%s of period has stored intervals').format(
							(percent < 0.01 ? '<0.01' : percent.toLocaleString(undefined, { maximumFractionDigits: 2 })) + '%') : _('No stored intervals');
						bands.forEach(band => band.points.forEach(function(point) {
							if (!rows.has(point[0])) rows.set(point[0], [null, null]);
							rows.get(point[0])[direction] = point[1];
						}));
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
		wrapper.appendChild(E('p', { class: 'traffic-footnote' }, _('Totals estimate recorded usage from stored average rates, excluding missing intervals. Older history is less detailed. Hover a chart to inspect a sample. Traffic staying within a switch, access point or bridge is outside these counters.')));
		return wrapper;
	},

	handleSave: null,
	handleSaveApply: null,
	handleReset: null
});
