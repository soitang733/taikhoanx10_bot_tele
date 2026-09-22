"use strict";

const assert = require("node:assert/strict");
const tools = require("./chart_tools.js");

assert.equal(tools.adjustedQuotePrice(120000, 90000, 100000), 108000);
assert.equal(tools.adjustedQuotePrice(120000, 90000, 0), null);
assert.equal(tools.sma([1, 2, 3, 4], 2).at(-1).value, 3.5);
assert.equal(tools.rsi(Array.from({ length: 20 }, (_, i) => i), 14).at(-1).value, 100);

const lines = [];
const updates = [];
let chartOptions;
const makeSeries = () => ({
  setData() {}, update(value) { updates.push(value); },
  priceScale() { return { applyOptions() {} }; },
  createPriceLine(options) { const line = { options }; lines.push(line); return line; },
  removePriceLine(line) { const index = lines.indexOf(line); if (index >= 0) lines.splice(index, 1); },
});
globalThis.LightweightCharts = {
  CandlestickSeries: {}, HistogramSeries: {}, LineSeries: {},
  LineStyle: { Solid: 0, Dashed: 2 },
  createChart(_container, options) { chartOptions = options; return {
    addSeries: makeSeries, removeSeries() {}, subscribeClick() {},
    timeScale() { return { fitContent() {} }; }, applyOptions() {}, remove() {},
  }; },
};
globalThis.localStorage = { getItem() { return null; }, setItem() {} };
globalThis.ResizeObserver = class { observe() {} disconnect() {} };

const rows = Array.from({ length: 220 }, (_, index) => ({
  date: new Date(Date.UTC(2025, 0, index + 1)).toISOString().slice(0, 10),
  open: 100000, high: 101000, low: 99000, close: 100000,
  adjusted_open: 90000, adjusted_high: 90900, adjusted_low: 89100,
  adjusted_close: 90000, volume: 1000,
}));
const chart = tools.create({ clientWidth: 600, clientHeight: 400 }, "FPT", rows,
  { ma: true, bb: true, rsi: true }, () => {});
assert.equal(chartOptions.layout.attributionLogo, false);
const session = { date: "2026-09-22", open: 110000, high: 121000,
                  low: 109000, close: 120000, volume: 2000 };
assert.equal(tools.normalizeSessionCandle(session, { time: "2026-09-21", close: 90000,
  rawClose: 100000 }).close, 108000);
assert.equal(tools.normalizeSessionCandle(session, { time: "2026-09-22", close: 90000,
  rawClose: 100000 }), null);
assert.equal(chart.updateSessionCandle(session).changed, true);
assert.equal(updates.at(-2).time, "2026-09-22");
assert.equal(updates.at(-2).color, "#f2b12e");
assert.equal(updates.at(-1).value, 2000);
const updateCount = updates.length;
assert.equal(chart.updateSessionCandle(session).changed, false);
assert.equal(updates.length, updateCount);
assert.equal(chart.updateQuote({ fresh: true, price_vnd: 120000 }), 108000);
assert.equal(lines.length, 1);
assert.equal(lines[0].options.price, 108000);
assert.equal(chart.updateQuote({ fresh: false, price_vnd: 120000 }), null);
assert.equal(lines.length, 0);
chart.destroy();
console.log("CHART_TOOLS_OK");
