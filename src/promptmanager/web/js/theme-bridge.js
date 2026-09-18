/**
 * Theme Bridge — sets the data-pm-theme attribute used by CSS selectors that
 * need to distinguish light vs dark.
 *
 * The preference is stored in localStorage under "pm-theme" ("light" or
 * "dark"); dark is the default.
 */
(function () {
  'use strict';

  function storedTheme() {
    try {
      return localStorage.getItem('pm-theme') === 'light' ? 'light' : 'dark';
    } catch {
      return 'dark';
    }
  }

  // Run immediately — script is loaded in <head> so body isn't parsed yet.
  // We set the attribute as soon as possible to prevent flash.
  document.documentElement.setAttribute('data-pm-theme', storedTheme());
})();
