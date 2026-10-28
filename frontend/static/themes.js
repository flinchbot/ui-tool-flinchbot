/* UniFi × flinchbot — theme manager.
   Loaded in <head> so the saved theme is applied before the page paints.
   Theme + light/dark preference are stored per browser (localStorage). */
(function () {
  'use strict';
  var GF = 'https://fonts.googleapis.com/css2?family=';
  var THEMES = [
    { id: 'default',     name: 'Default',        desc: 'The original flinchbot look.',
      fonts: 'Syne:wght@400;600;800&family=Space+Mono:wght@400;700' },
    { id: 'matrix',      name: 'Matrix',         desc: 'Green phosphor terminal with digital rain.',
      fonts: 'Share+Tech+Mono&family=VT323' },
    { id: 'win95',       name: 'Windows 95',     desc: 'Bevelled windows, tooltips and a taskbar.',
      fonts: null },
    { id: 'starwars',    name: 'Star Wars',      desc: 'Starfield, crawl yellow, status strip. Fan-inspired.',
      fonts: 'Orbitron:wght@500;700&family=Exo+2:wght@400;600&family=Share+Tech+Mono' },
    { id: 'mlp',         name: 'My Little Pony', desc: 'Pastels, rainbow trim, round and bubbly. Fan-inspired.',
      fonts: 'Fredoka:wght@400;500;600' },
    { id: 'bladerunner', name: 'Blade Runner',   desc: 'Neon noir, rain and angled HUD panels. Fan-inspired.',
      fonts: 'Rajdhani:wght@400;500;600&family=Oxanium:wght@500;600&family=Share+Tech+Mono' },
    { id: 'candle',      name: 'Candle Maker',   desc: 'Warm wax, amber flame glow, cosy serif.',
      fonts: 'Lora:ital,wght@0,400;0,600;1,400&family=Cormorant+Garamond:ital,wght@0,600;0,700;1,600&family=Courier+Prime' },
    { id: 'notredame',   name: 'Notre Dame Football', desc: 'Navy and gold, yard lines, scoreboard stats. Fan-inspired.',
      fonts: 'Oswald:wght@500;600;700&family=Source+Sans+3:wght@400;600&family=Roboto+Mono:wght@400;500' },
    { id: 'bayern',      name: 'FC Bayern München', desc: 'Red and white stripes, the arena glowing at night. Fan-inspired.',
      fonts: 'Barlow+Condensed:wght@500;600;700&family=Barlow:wght@400;500;600&family=JetBrains+Mono:wght@400;500' },
    { id: 'arcade',      name: '80s Arcade',     desc: '8-bit pixel type, CRT scanlines, neon on black. Insert coin.',
      fonts: 'Press+Start+2P&family=VT323' },
    { id: 'oktoberfest', name: 'Oktoberfest',    desc: 'Blue-and-white diamonds, beer gold, pretzel brown. Prost!',
      fonts: 'UnifrakturMaguntia&family=Alegreya+Sans:wght@400;500;700&family=Alegreya+Sans+SC:wght@500;700&family=Courier+Prime' },
  ];
  var KEY_T = 'flinchbot.theme', KEY_M = 'flinchbot.mode';
  var html = document.documentElement;
  var mq = window.matchMedia ? window.matchMedia('(prefers-color-scheme: light)') : null;
  var reduce = window.matchMedia ? window.matchMedia('(prefers-reduced-motion: reduce)') : null;

  function load(k, d) { try { return localStorage.getItem(k) || d; } catch (e) { return d; } }
  function save(k, v) { try { localStorage.setItem(k, v); } catch (e) { /* private mode etc. */ } }
  function byId(id) { for (var i = 0; i < THEMES.length; i++) if (THEMES[i].id === id) return THEMES[i]; return THEMES[0]; }
  function resolve(pref) { return pref === 'light' || pref === 'dark' ? pref : (mq && mq.matches ? 'light' : 'dark'); }

  function loadFonts(t) {
    if (!t || !t.fonts || document.getElementById('font-' + t.id)) return;
    var l = document.createElement('link');
    l.id = 'font-' + t.id; l.rel = 'stylesheet'; l.href = GF + t.fonts + '&display=swap';
    document.head.appendChild(l);
  }

  var state = { theme: byId(load(KEY_T, 'default')).id, pref: load(KEY_M, 'auto') };

  function apply(themeId, pref, persist) {
    if (themeId) state.theme = byId(themeId).id;
    if (pref) state.pref = pref;
    html.setAttribute('data-theme', state.theme);
    html.setAttribute('data-mode', resolve(state.pref));
    html.setAttribute('data-mode-pref', state.pref);
    loadFonts(byId(state.theme));
    if (persist !== false) { save(KEY_T, state.theme); save(KEY_M, state.pref); }
    effects();
  }

  // ── Matrix digital rain (canvas inside #fx) ────────────────────────────────
  var rain = null;
  function stopRain() { if (rain) { cancelAnimationFrame(rain.raf); rain.canvas.remove(); rain = null; } }
  function startRain() {
    var fx = document.getElementById('fx');
    if (!fx || rain) return;
    var c = document.createElement('canvas'), ctx = c.getContext('2d');
    c.style.cssText = 'position:absolute;inset:0;width:100%;height:100%';
    fx.appendChild(c);
    var glyphs = 'ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜﾝ0123456789:.=*+-<>¦';
    var size = 16, cols = 0, drops = [], last = 0;
    function resize() {
      c.width = innerWidth; c.height = innerHeight;
      cols = Math.ceil(c.width / size);
      drops = []; for (var i = 0; i < cols; i++) drops.push(Math.random() * -60);
    }
    resize(); addEventListener('resize', resize);
    function draw() {
      var light = html.getAttribute('data-mode') === 'light';
      ctx.fillStyle = light ? 'rgba(233,246,234,0.18)' : 'rgba(0,3,0,0.12)';
      ctx.fillRect(0, 0, c.width, c.height);
      ctx.font = size + 'px "Share Tech Mono", monospace';
      for (var i = 0; i < cols; i++) {
        var ch = glyphs[(Math.random() * glyphs.length) | 0], y = drops[i] * size;
        ctx.fillStyle = light ? 'rgba(6,120,42,0.55)' : (Math.random() < 0.06 ? '#d8ffe0' : 'rgba(0,255,65,0.75)');
        ctx.fillText(ch, i * size, y);
        if (y > c.height && Math.random() > 0.975) drops[i] = 0;
        drops[i] += 1;
      }
    }
    function frame(t) {
      rain.raf = requestAnimationFrame(frame);
      if (document.hidden || t - last < 55) return;          // ~18 fps, paused in background tabs
      last = t;
      draw();
    }
    rain = { canvas: c, raf: 0 };
    if (reduce && reduce.matches) {                           // reduced motion: one still frame
      for (var k = 0; k < 45; k++) draw();
    } else {
      rain.raf = requestAnimationFrame(frame);
    }
  }
  function effects() {
    if (!document.getElementById('fx')) return;               // called again on DOMContentLoaded
    if (state.theme === 'matrix') startRain(); else stopRain();
  }

  if (mq && mq.addEventListener) mq.addEventListener('change', function () { if (state.pref === 'auto') apply(null, null, false); });
  document.addEventListener('DOMContentLoaded', effects);

  window.Themes = {
    list: THEMES,
    apply: apply,
    current: function () { return { theme: state.theme, pref: state.pref, mode: resolve(state.pref) }; },
    preloadAllFonts: function () { THEMES.forEach(loadFonts); },
  };
  apply(null, null, false);
})();
