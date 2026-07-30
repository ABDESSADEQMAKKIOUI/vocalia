/**
 * Volira Voice Widget — legacy path shim
 *
 * The embeddable widget now ships as volira-widget.js. This file stays in place
 * because /embed/dograh-widget.js is already pasted into customer sites and
 * those pages must keep working untouched.
 *
 * It is a loader rather than a duplicate of the widget on purpose: there is
 * exactly one implementation to fix and deploy, so anything fixed in
 * volira-widget.js reaches legacy embeds too instead of the two copies quietly
 * drifting apart.
 *
 * Version: 1.0.0
 */

(function () {
  'use strict';

  var WIDGET_FILE = 'volira-widget.js';
  var WIDGET_SCRIPT_ID = 'volira-widget';

  // The widget is already loading or loaded — e.g. the page carries both the
  // legacy snippet and the current one. Loading it twice would render two
  // widgets and fight over the same global.
  if (window.VoliraWidget || document.getElementById(WIDGET_SCRIPT_ID)) return;

  // Resolve our own <script> tag: its query string carries the embed token and
  // API endpoint, and the widget reads both off the script it was loaded from.
  var hostScript = document.currentScript
    || document.querySelector('script[src*="dograh-widget.js"]');

  if (!hostScript || !hostScript.src) {
    console.error('Volira Widget: could not locate the embed script tag');
    return;
  }

  var widgetSrc;
  try {
    var url = new URL(hostScript.src, window.location.href);
    // Swap only the filename, keeping the origin, directory and query string.
    url.pathname = url.pathname.replace(/[^/]*$/, WIDGET_FILE);
    widgetSrc = url.toString();
  } catch (error) {
    console.error('Volira Widget: could not resolve the widget URL', error);
    return;
  }

  var script = document.createElement('script');
  script.id = WIDGET_SCRIPT_ID;
  script.src = widgetSrc;
  script.async = true;

  // Forward host-page configuration set on the script tag (e.g.
  // data-dograh-context), which the widget reads from its own element.
  for (var i = 0; i < hostScript.attributes.length; i++) {
    var attribute = hostScript.attributes[i];
    if (attribute.name.indexOf('data-') === 0) {
      script.setAttribute(attribute.name, attribute.value);
    }
  }

  (document.head || document.documentElement).appendChild(script);
})();
