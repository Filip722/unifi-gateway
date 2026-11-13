'use strict';
'require baseclass';

return baseclass.extend({

	title: _('Device traffic'),

	rrdargs(graph, host, plugin, instance) {
		return ['internet', 'local'].map(function(source) {
			const options = {};
			options['bytes_' + source + '_download'] = {
				title: _('Download'), color: '0088ff', overlay: true
			};
			options['bytes_' + source + '_upload'] = {
				title: _('Upload'), color: 'a855f7', noarea: true, overlay: true
			};
			return {
				title: '%H: %pi - ' + (source === 'internet' ? _('Internet') : _('Routed LAN/VLAN')),
				vlabel: _('Bytes/s'),
				data: {
					instances: { bytes: [source + '_download', source + '_upload'] },
					options: options
				}
			};
		});
	}
});
