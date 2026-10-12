/* 政治地理マップ ビューア
 * data/municipalities.topojson … 市区町村ポリゴン
 * data/data.json               … 地域ごとの値（v[axis] = [value, z, rank, denominator, small, excluded]）
 * data/meta.json               … 軸・主成分・クラスターの説明
 */
(() => {
  "use strict";

  const DATA_DIR = new URLSearchParams(location.search).get("data") || "data";
  const FIXED_BREAKS = [-1.5, -1, -0.5, 0.5, 1, 1.5]; // ±0.5σ刻み（中央 -0.5〜0.5 は中立）
  const CLIP = 3;

  // 色はダーク/ライトそれぞれで定義する（発散型は 青 ↔ 赤、中央はグレー）
  const PALETTES = {
    light: {
      diverging: ["#184f95", "#3987e5", "#9ec5f4", "#e1e0d9", "#f4b0a8", "#e34948", "#a32323"],
      bivariate: [ // [縦の段][横の段]（低→高）
        ["#e8e8e8", "#e4acac", "#c85a5a"],
        ["#b0d5df", "#ad9ea5", "#985356"],
        ["#64acbe", "#627f8c", "#574249"],
      ],
      categorical: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948", "#7a7974", "#00a3b5"],
      noData: "#a9a8a2", outline: "#fcfcfb", hover: "#0b0b0b", hatch: "rgba(11,11,11,0.55)",
    },
    dark: {
      diverging: ["#9ec5f4", "#3987e5", "#1c5cab", "#55544f", "#a83a36", "#e66767", "#f4b0a8"],
      bivariate: [
        ["#4a4a46", "#8f4a4a", "#d06a6a"],
        ["#3f7f8f", "#7a6a70", "#b06065"],
        ["#64acbe", "#7f9aa6", "#c9a0a6"],
      ],
      categorical: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767", "#a3a29a", "#1fb8c9"],
      noData: "#2c2c2a", outline: "#1a1a19", hover: "#f0efec", hatch: "rgba(240,239,236,0.55)",
    },
  };

  const state = {
    mode: "axis", axis1: null, axis2: null, pc: 0, breaks: "fixed", hatch: true,
    selected: null, isolate: null,
  };
  let META, AREAS, GEO, map, axisById = {};

  const $ = (id) => document.getElementById(id);
  const fmt = (x, d = 1) => (x == null || Number.isNaN(x) ? "—" : x.toLocaleString("ja-JP", { maximumFractionDigits: d, minimumFractionDigits: d }));
  const pct = (x) => (x == null ? "—" : `${fmt(x * 100, 1)}%`);
  const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const theme = () => {
    const t = document.documentElement.dataset.theme;
    if (t === "dark" || t === "light") return t;
    return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  };
  const pal = () => PALETTES[theme()];

  // ------------------------------------------------------------ 値の取り出し

  function axisRec(code, axisId) {
    const a = AREAS[code];
    const v = a && a.v && a.v[axisId];
    if (!v) return null;
    const [value, z, rank, den, small, excluded] = v;
    const ax = axisById[axisId];
    // 分析から除外された地域も、地図上は元の値の位置を表示する（斜線で注記）
    let zd = z;
    if (zd == null && value != null && ax.std) zd = Math.max(-CLIP, Math.min(CLIP, (value - ax.mean) / ax.std));
    return { value, z, zd, rank, den, small: !!small, excluded: !!excluded };
  }

  function smallAny(code) {
    const a = AREAS[code];
    return !!(a && a.v && Object.values(a.v).some((v) => v[4]));
  }

  function formatValue(ax, v) {
    if (v == null) return "—";
    if (ax.formula === "ratio") return pct(v);
    if (ax.formula === "growth") return `${v >= 0 ? "+" : ""}${fmt(v * 100, 1)}%`;
    return `${v >= 0 ? "+" : ""}${fmt(v * 100, 1)}pt`;
  }

  const pcStd = [];
  function pcScore(code, i) {
    const a = AREAS[code];
    return a && a.pc ? a.pc[i] : null;
  }

  // ------------------------------------------------------------ 階級区分

  function quantileBreaks(values, n) {
    const xs = values.filter((x) => x != null).sort((a, b) => a - b);
    if (!xs.length) return FIXED_BREAKS;
    const out = [];
    for (let i = 1; i < n; i++) out.push(xs[Math.min(xs.length - 1, Math.floor((i * xs.length) / n))]);
    return out;
  }

  function classify(x, breaks) {
    if (x == null || Number.isNaN(x)) return -1;
    let i = 0;
    while (i < breaks.length && x >= breaks[i]) i++;
    return i;
  }

  // ------------------------------------------------------------ 塗り分け

  function currentMetric() {
    // 返り値: {get(code) → 数値（σ単位）, breaks, labelL, labelR, title}
    const codes = Object.keys(AREAS);
    if (state.mode === "pca") {
      const p = META.pca[state.pc];
      const sd = pcStd[state.pc] || 1;
      const get = (c) => { const s = pcScore(c, state.pc); return s == null ? null : s / sd; };
      const breaks = state.breaks === "fixed" ? FIXED_BREAKS : quantileBreaks(codes.map(get), 7);
      return { get, breaks, left: p.left, right: p.right, title: `${p.id}: ${p.name}${p.provisional ? "（仮の名前）" : ""}` };
    }
    const ax = axisById[state.axis1];
    const get = (c) => { const r = axisRec(c, ax.id); return r ? r.zd : null; };
    const breaks = state.breaks === "fixed" ? FIXED_BREAKS : quantileBreaks(codes.map(get), 7);
    return { get, breaks, left: ax.left, right: ax.right, title: ax.name };
  }

  function tertiles(axisId) {
    const xs = Object.keys(AREAS).map((c) => { const r = axisRec(c, axisId); return r ? r.zd : null; });
    return quantileBreaks(xs, 3);
  }

  function paint() {
    const P = pal();
    let colorOf, hatchOf;
    if (state.mode === "bivariate") {
      const b1 = tertiles(state.axis1), b2 = tertiles(state.axis2);
      colorOf = (c) => {
        const r1 = axisRec(c, state.axis1), r2 = axisRec(c, state.axis2);
        const i = classify(r1 && r1.zd, b1), j = classify(r2 && r2.zd, b2);
        return i < 0 || j < 0 ? P.noData : P.bivariate[j][i];
      };
      hatchOf = (c) => !!((axisRec(c, state.axis1) || {}).small || (axisRec(c, state.axis2) || {}).small);
    } else if (state.mode === "cluster") {
      colorOf = (c) => {
        const k = AREAS[c] && AREAS[c].cl;
        return k ? P.categorical[(k - 1) % P.categorical.length] : P.noData;
      };
      hatchOf = smallAny;
    } else {
      const m = currentMetric();
      colorOf = (c) => {
        const i = classify(m.get(c), m.breaks);
        return i < 0 ? P.noData : P.diverging[i];
      };
      hatchOf = state.mode === "pca" ? smallAny : (c) => !!(axisRec(c, state.axis1) || {}).small;
    }
    for (const f of GEO.features) {
      const c = f.properties.code;
      const known = !!AREAS[c];
      f.properties._c = known ? colorOf(c) : P.noData;
      f.properties._h = known && state.hatch && hatchOf(c) ? 1 : 0;
      f.properties._o = state.mode === "cluster" && state.isolate && AREAS[c] && AREAS[c].cl !== state.isolate ? 0.18 : 1;
    }
    map.getSource("areas").setData(GEO);
    map.setPaintProperty("fill-outline", "line-color", P.outline);
    map.setPaintProperty("hover", "line-color", P.hover);
    renderLegend();
    renderInfo();
    renderTable();
    renderProfile();
  }

  // ------------------------------------------------------------ 凡例

  function rampLegend(m, unit) {
    const P = pal();
    const sw = P.diverging.map((col) => `<span style="background:${col}"></span>`).join("");
    const ticks = m.breaks.map((b) => `<span>${state.breaks === "fixed" ? (b > 0 ? "+" : "") + b : fmt(b, 2)}</span>`).join("");
    return `
      <h2>${esc(m.title)}</h2>
      <div class="ramp">${sw}</div>
      <div class="ticks" style="padding:0 calc(100% / 14)">${ticks}</div>
      <div class="ramp-labels"><span>← <b>${esc(m.left)}</b></span><span><b>${esc(m.right)}</b> →</span></div>
      <p class="note">${unit}。${state.breaks === "fixed" ? "中央の灰色は ±0.5σ 以内" : "各区分の地域数が等しくなる分位点"}。</p>`;
  }

  function commonNotes() {
    const sp = META.small_population || {};
    return `<p class="note"><span class="swatch-na"></span> データなし・所属未定地　` +
      (state.hatch ? `<span class="swatch-hatch"></span> 小人口（分母 ${fmt(sp.threshold || 0, 0)} 未満）` : "") + `</p>`;
  }

  function renderLegend() {
    const el = $("legend");
    const P = pal();
    if (state.mode === "axis") {
      el.innerHTML = rampLegend(currentMetric(), "zスコア（σ）") + commonNotes();
    } else if (state.mode === "pca") {
      el.innerHTML = rampLegend(currentMetric(), "主成分スコア（標準偏差単位）") + commonNotes();
    } else if (state.mode === "bivariate") {
      const a1 = axisById[state.axis1], a2 = axisById[state.axis2];
      const cells = [2, 1, 0].map((j) => [0, 1, 2].map((i) => `<span style="background:${P.bivariate[j][i]}"></span>`).join("")).join("");
      el.innerHTML = `
        <h2>2軸の重ね合わせ（各軸を3分位）</h2>
        <div class="bivar">
          <div class="grid">${cells}</div>
          <div class="labs">
            <p>↑ 縦: ${esc(a2.left)} → <b>${esc(a2.right)}</b></p>
            <p>→ 横: ${esc(a1.left)} → <b>${esc(a1.right)}</b></p>
          </div>
        </div>${commonNotes()}`;
    } else {
      const items = META.clusters.map((c) => {
        const col = P.categorical[(c.id - 1) % P.categorical.length];
        return `<li><button data-k="${c.id}" aria-pressed="${state.isolate === c.id}">
          <span class="dot" style="background:${col}"></span>${c.id}. ${esc(c.name)}<span class="n">${c.n}</span></button></li>`;
      }).join("");
      el.innerHTML = `<h2>地域類型（k-means, k=${META.clusters.length}）</h2><ul class="clusters">${items}</ul>
        <p class="note">類型をクリックするとその類型だけを強調します。</p>${commonNotes()}`;
      el.querySelectorAll("button[data-k]").forEach((b) => b.addEventListener("click", () => {
        const k = +b.dataset.k;
        state.isolate = state.isolate === k ? null : k;
        paint();
      }));
    }
  }

  function renderInfo() {
    const el = $("info");
    if (state.mode === "pca") {
      const p = META.pca[state.pc];
      const rows = Object.entries(p.loadings).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]))
        .map(([id, l]) => `<tr><td>${esc(axisById[id] ? axisById[id].name : id)}</td><td>${l >= 0 ? "+" : ""}${fmt(l, 2)}</td></tr>`).join("");
      el.innerHTML = `<h2>${p.id} の負荷量</h2>
        <p>寄与率 ${pct(p.explained)}${p.provisional ? "。名前は負荷量最大の軸から付けた仮の名前です（settings.yaml の pca.names で変更）" : ""}</p>
        <table class="loadings">${rows}</table>`;
    } else if (state.mode === "cluster") {
      const k = state.isolate || (state.selected && AREAS[state.selected] && AREAS[state.selected].cl);
      const c = k && META.clusters.find((x) => x.id === k);
      el.innerHTML = c ? `<h2>${c.id}. ${esc(c.name)} の特徴（全体平均との差）</h2>${barsSvg(META.axes.map((a) => ({ label: a.name, z: c.z[a.id] })))}` : "";
    } else {
      const axs = state.mode === "bivariate" ? [state.axis1, state.axis2] : [state.axis1];
      el.innerHTML = axs.map((id) => {
        const a = axisById[id];
        return `<p><b>${esc(a.name)}</b>（${esc(a.group)}・${{ ratio: "比率", difference: "比率の差", growth: "増減率" }[a.formula]}）。
          平均 ${formatValue(a, a.mean)}、標準偏差 ${formatValue(a, a.std).replace(/^\+/, "")}${a.known_bias ? `。注記: ${esc(a.known_bias)}` : ""}</p>`;
      }).join("");
    }
  }

  // ------------------------------------------------------------ プロフィール（全軸の zスコア）

  function barsSvg(rows) {
    // 各行: 1行目に軸名（全文）、2行目に zスコアの棒。右に伸びるほど軸名の「↔」の右側
    const W = 328, R = 44, H = 36, top = 18;
    const x = (z) => ((Math.max(-CLIP, Math.min(CLIP, z)) + CLIP) / (2 * CLIP)) * (W - R);
    const P = pal();
    const neg = P.diverging[1], pos = P.diverging[5];
    const bottom = top + rows.length * H;
    let s = `<svg viewBox="0 0 ${W} ${bottom + 4}" role="img" aria-label="平均との差（zスコア）">`;
    for (const t of [-3, -2, -1, 0, 1, 2, 3]) {
      s += `<line x1="${x(t)}" x2="${x(t)}" y1="${top - 4}" y2="${bottom}" stroke="var(${t === 0 ? "--axis" : "--grid"})" stroke-width="1"/>`;
      s += `<text x="${Math.min(W - R - 8, Math.max(8, x(t)))}" y="11" text-anchor="middle">${t > 0 ? "+" : ""}${t}σ</text>`;
    }
    rows.forEach((r, i) => {
      const y = top + i * H;
      s += `<text x="0" y="${y + 12}">${esc(r.label)}</text>`;
      if (r.z == null) {
        s += `<text x="${W}" y="${y + 29}" text-anchor="end" class="val">—</text>`;
        return;
      }
      const x0 = x(0), x1 = x(r.z);
      const w = Math.max(2, Math.abs(x1 - x0));
      s += `<rect x="${Math.min(x0, x1)}" y="${y + 18}" width="${w}" height="12" rx="3" fill="${r.z >= 0 ? pos : neg}"><title>${esc(r.label)}: ${fmt(r.z, 2)}σ</title></rect>`;
      s += `<text x="${W}" y="${y + 29}" text-anchor="end" class="val">${r.z >= 0 ? "+" : ""}${fmt(r.z, 2)}</text>`;
    });
    return s + "</svg>";
  }

  function renderProfile() {
    const el = $("profile");
    const c = state.selected;
    if (!c || !AREAS[c]) return;
    const a = AREAS[c];
    const rows = META.axes.map((ax) => {
      const r = axisRec(c, ax.id);
      return { label: ax.name, z: r ? r.zd : null };
    });
    const cl = a.cl && META.clusters ? META.clusters.find((x) => x.id === a.cl) : null;
    el.innerHTML = `<h2>${esc(a.pref)} ${esc(a.name)} のプロフィール</h2>
      <p class="sub">全軸の zスコア（平均との差、σ単位）。右ほど軸名の「↔」の右側${cl ? `・地域類型 ${cl.id}. ${esc(cl.name)}` : ""}</p>
      ${barsSvg(rows)}`;
  }

  // ------------------------------------------------------------ 一覧表

  function renderTable() {
    const el = $("table");
    const codes = Object.keys(AREAS);
    let rows, head;
    if (state.mode === "cluster") {
      head = "<th>市区町村</th><th class='num'>類型</th>";
      rows = codes.filter((c) => AREAS[c].cl).sort((a, b) => AREAS[a].cl - AREAS[b].cl)
        .map((c) => `<tr><td>${esc(AREAS[c].name)}</td><td class="num">${AREAS[c].cl}</td></tr>`);
    } else if (state.mode === "pca") {
      head = `<th>市区町村</th><th class="num">${META.pca[state.pc].id} スコア</th>`;
      rows = codes.filter((c) => pcScore(c, state.pc) != null).sort((a, b) => pcScore(b, state.pc) - pcScore(a, state.pc))
        .map((c) => `<tr><td>${esc(AREAS[c].name)}</td><td class="num">${fmt(pcScore(c, state.pc), 2)}</td></tr>`);
    } else {
      const ax = axisById[state.axis1];
      head = `<th>順位</th><th>市区町村</th><th class="num">値</th><th class="num">z</th><th class="num">分母</th>`;
      rows = codes.map((c) => [c, axisRec(c, ax.id)]).filter(([, r]) => r && r.value != null)
        .sort((a, b) => a[1].rank - b[1].rank)
        .map(([c, r]) => `<tr><td>${r.rank}</td><td>${esc(AREAS[c].name)}${r.small ? " *" : ""}</td><td class="num">${formatValue(ax, r.value)}</td><td class="num">${fmt(r.zd, 2)}</td><td class="num">${fmt(r.den, 0)}</td></tr>`);
    }
    el.innerHTML = `<table><thead><tr>${head}</tr></thead><tbody>${rows.join("")}</tbody></table>`;
  }

  // ------------------------------------------------------------ ツールチップ

  function tooltipHtml(code, name) {
    const a = AREAS[code];
    if (!a) return `<h3>${esc(name || "所属未定地")}</h3><p class="flag">データなし</p>`;
    const dl = (pairs) => `<dl>${pairs.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("")}</dl>`;
    const axisBlock = (id) => {
      const ax = axisById[id];
      const r = axisRec(code, id);
      if (!r) return `<p class="flag">${esc(ax.name)}: データなし</p>`;
      return dl([
        ["元の値", formatValue(ax, r.value)],
        ["zスコア", r.zd == null ? "—" : `${r.zd >= 0 ? "+" : ""}${fmt(r.zd, 2)}`],
        ["順位", r.rank ? `${r.rank} / ${ax.rank_of}` : "—"],
        ["分母", `${fmt(r.den, 0)} 人`],
      ]) + (r.small ? `<p class="flag">小人口のため信頼性が低い${r.excluded ? "（統合分析から除外）" : ""}</p>` : "");
    };
    let body = "";
    if (state.mode === "axis") body = axisBlock(state.axis1);
    else if (state.mode === "bivariate") body = `<p class="flag">${esc(axisById[state.axis1].name)}</p>${axisBlock(state.axis1)}<p class="flag">${esc(axisById[state.axis2].name)}</p>${axisBlock(state.axis2)}`;
    else if (state.mode === "pca") {
      const s = pcScore(code, state.pc);
      body = s == null ? `<p class="flag">統合分析の対象外（欠損または小人口）</p>` : dl([[`${META.pca[state.pc].id} スコア`, fmt(s, 2)]]);
    } else {
      const cl = a.cl && META.clusters.find((x) => x.id === a.cl);
      body = cl ? dl([["地域類型", `${cl.id}. ${esc(cl.name)}`]]) : `<p class="flag">統合分析の対象外（欠損または小人口）</p>`;
    }
    return `<h3>${esc(a.name)} <span class="muted">${esc(a.pref)}</span></h3>${body}`;
  }

  // ------------------------------------------------------------ 地図

  function hatchImage() {
    const size = 16, ratio = 2;
    const cv = document.createElement("canvas");
    cv.width = cv.height = size;
    const g = cv.getContext("2d");
    g.strokeStyle = pal().hatch;
    g.lineWidth = 1.5;
    for (const o of [-size, 0, size]) {
      g.beginPath();
      g.moveTo(o, size);
      g.lineTo(o + size, 0);
      g.stroke();
    }
    return { image: g.getImageData(0, 0, size, size), ratio };
  }

  function bbox(fc) {
    let [x0, y0, x1, y1] = [Infinity, Infinity, -Infinity, -Infinity];
    const walk = (c) => {
      if (typeof c[0] === "number") { x0 = Math.min(x0, c[0]); y0 = Math.min(y0, c[1]); x1 = Math.max(x1, c[0]); y1 = Math.max(y1, c[1]); }
      else c.forEach(walk);
    };
    fc.features.forEach((f) => f.geometry && walk(f.geometry.coordinates));
    return [[x0, y0], [x1, y1]];
  }

  function initMap() {
    const surface = getComputedStyle(document.documentElement).getPropertyValue("--page").trim() || "#f9f9f7";
    map = new maplibregl.Map({
      container: "map",
      style: { version: 8, sources: {}, layers: [{ id: "bg", type: "background", paint: { "background-color": surface } }] },
      bounds: bbox(GEO),
      fitBoundsOptions: { padding: 24 },
      attributionControl: { customAttribution: "境界: 国土数値情報（行政区域）／ 統計: 国勢調査（e-Stat）" },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    map.on("load", () => {
      const h = hatchImage();
      map.addImage("hatch", h.image, { pixelRatio: h.ratio });
      map.addSource("areas", { type: "geojson", data: GEO });
      map.addLayer({ id: "fill", type: "fill", source: "areas", paint: { "fill-color": ["get", "_c"], "fill-opacity": ["get", "_o"] } });
      map.addLayer({ id: "hatch", type: "fill", source: "areas", filter: ["==", ["get", "_h"], 1], paint: { "fill-pattern": "hatch", "fill-opacity": ["get", "_o"] } });
      map.addLayer({ id: "fill-outline", type: "line", source: "areas", paint: { "line-color": "#fff", "line-width": 0.6 } });
      map.addLayer({ id: "hover", type: "line", source: "areas", filter: ["==", ["get", "code"], ""], paint: { "line-color": "#000", "line-width": 2 } });
      map.addLayer({ id: "selected", type: "line", source: "areas", filter: ["==", ["get", "code"], ""], paint: { "line-color": "#2a78d6", "line-width": 3 } });

      const tip = $("tooltip");
      map.on("mousemove", "fill", (e) => {
        const f = e.features[0];
        map.getCanvas().style.cursor = "pointer";
        map.setFilter("hover", ["==", ["get", "code"], f.properties.code]);
        tip.innerHTML = tooltipHtml(f.properties.code, f.properties.name);
        tip.hidden = false;
        const { x, y } = e.point;
        const box = map.getContainer().getBoundingClientRect();
        const w = tip.offsetWidth, hh = tip.offsetHeight;
        tip.style.left = `${x + 14 + w > box.width ? x - w - 14 : x + 14}px`;
        tip.style.top = `${y + 14 + hh > box.height ? Math.max(0, y - hh - 14) : y + 14}px`;
      });
      map.on("mouseleave", "fill", () => {
        map.getCanvas().style.cursor = "";
        map.setFilter("hover", ["==", ["get", "code"], ""]);
        tip.hidden = true;
      });
      map.on("click", "fill", (e) => {
        const code = e.features[0].properties.code;
        state.selected = code;
        map.setFilter("selected", ["==", ["get", "code"], code]);
        renderProfile();
        if (state.mode === "cluster") renderInfo();
      });
      paint();
      $("status").textContent = "";
    });
  }

  // ------------------------------------------------------------ 操作

  function syncControls() {
    document.querySelectorAll("#modes button").forEach((b) => b.setAttribute("aria-checked", String(b.dataset.mode === state.mode)));
    $("f-axis1").hidden = !(state.mode === "axis" || state.mode === "bivariate");
    $("f-axis1").querySelector("span").textContent = state.mode === "bivariate" ? "横の軸" : "軸";
    $("f-axis2").hidden = state.mode !== "bivariate";
    $("f-pc").hidden = state.mode !== "pca";
    $("f-breaks").hidden = !(state.mode === "axis" || state.mode === "pca");
  }

  function fillSelect(sel, items) {
    let group = null, html = "";
    for (const it of items) {
      if (it.group !== group) {
        if (group !== null) html += "</optgroup>";
        html += `<optgroup label="${esc(it.group)}">`;
        group = it.group;
      }
      html += `<option value="${esc(it.value)}">${esc(it.label)}</option>`;
    }
    sel.innerHTML = html + (group !== null ? "</optgroup>" : "");
  }

  function initControls() {
    const axItems = META.axes.map((a) => ({ value: a.id, label: a.name, group: a.group }));
    fillSelect($("axis1"), axItems);
    fillSelect($("axis2"), axItems);
    state.axis1 = META.axes[0].id;
    state.axis2 = (META.axes[1] || META.axes[0]).id;
    $("axis2").value = state.axis2;
    if (META.pca) {
      fillSelect($("pc"), META.pca.map((p, i) => ({ value: i, label: `${p.id}（${pct(p.explained)}）: ${p.name}`, group: "主成分" })));
      META.pca.forEach((p, i) => {
        const xs = Object.values(AREAS).map((a) => (a.pc ? a.pc[i] : null)).filter((x) => x != null);
        const m = xs.reduce((s, x) => s + x, 0) / xs.length;
        pcStd[i] = Math.sqrt(xs.reduce((s, x) => s + (x - m) ** 2, 0) / xs.length) || 1;
      });
    } else {
      document.querySelectorAll('#modes [data-mode="pca"], #modes [data-mode="cluster"]').forEach((b) => (b.disabled = true));
    }
    if (META.axes.length < 2) document.querySelector('#modes [data-mode="bivariate"]').disabled = true;

    document.querySelectorAll("#modes button").forEach((b) => b.addEventListener("click", () => {
      state.mode = b.dataset.mode;
      syncControls();
      paint();
    }));
    $("axis1").addEventListener("change", (e) => { state.axis1 = e.target.value; paint(); });
    $("axis2").addEventListener("change", (e) => { state.axis2 = e.target.value; paint(); });
    $("pc").addEventListener("change", (e) => { state.pc = +e.target.value; paint(); });
    $("breaks").addEventListener("change", (e) => { state.breaks = e.target.value; paint(); });
    $("hatch").addEventListener("change", (e) => { state.hatch = e.target.checked; paint(); });
    matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (!map) return;
      map.setPaintProperty("bg", "background-color", getComputedStyle(document.documentElement).getPropertyValue("--page").trim());
      map.updateImage("hatch", hatchImage().image);
      paint();
    });
    syncControls();
  }

  async function main() {
    $("status").textContent = "読み込み中…";
    try {
      const [topo, data, meta] = await Promise.all(
        ["municipalities.topojson", "data.json", "meta.json"].map((f) =>
          fetch(`${DATA_DIR}/${f}`).then((r) => { if (!r.ok) throw new Error(`${f}: ${r.status}`); return r.json(); }))
      );
      META = meta;
      AREAS = data.areas;
      META.axes.forEach((a) => (axisById[a.id] = a));
      GEO = topojson.feature(topo, topo.objects.municipalities);
      $("title").textContent = META.title;
      $("scope").textContent = `対象: ${Array.isArray(META.scope) ? META.scope.join(", ") : META.scope}・${Object.keys(AREAS).length} 地域`;
      document.title = META.title;
      initControls();
      initMap();
    } catch (err) {
      $("status").textContent = `データを読み込めません（${err.message}）。polmap run または polmap demo を実行してください。`;
      console.error(err);
    }
  }

  main();
})();
