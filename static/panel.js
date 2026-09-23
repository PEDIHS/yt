(() => {
  "use strict";

  const palette = ["#ff3654", "#8b5cf6", "#22d3ee", "#34d399", "#fbbf24", "#60a5fa", "#f472b6", "#a3e635"];
  const text = "#e9edf5";
  const muted = "#8791a6";
  const grid = "rgba(148, 163, 184, .11)";
  const tooltipBackground = "rgba(10, 13, 22, .96)";
  const charts = [];

  const readJSON = (id) => {
    const el = document.getElementById(id);
    if (!el) return null;
    try { return JSON.parse(el.textContent || "{}"); } catch (_) { return null; }
  };

  const compact = (value) => {
    const number = Number(value || 0);
    if (Math.abs(number) >= 1e9) return (number / 1e9).toFixed(1).replace(".0", "") + "B";
    if (Math.abs(number) >= 1e6) return (number / 1e6).toFixed(1).replace(".0", "") + "M";
    if (Math.abs(number) >= 1e3) return (number / 1e3).toFixed(1).replace(".0", "") + "K";
    return Math.round(number).toLocaleString();
  };

  const duration = (value) => {
    let seconds = Math.max(0, Math.round(Number(value || 0)));
    const minutes = Math.floor(seconds / 60);
    seconds %= 60;
    return minutes + ":" + String(seconds).padStart(2, "0");
  };

  const tooltip = {
    trigger: "axis",
    backgroundColor: tooltipBackground,
    borderColor: "rgba(255,255,255,.1)",
    borderWidth: 1,
    textStyle: { color: text, fontFamily: "Tahoma, Arial, sans-serif" },
    extraCssText: "box-shadow:0 18px 55px rgba(0,0,0,.38);backdrop-filter:blur(18px);border-radius:12px;padding:10px 12px;"
  };

  const axisLabel = { color: muted, fontSize: 11 };
  const splitLine = { lineStyle: { color: grid } };

  const makeChart = (id, option) => {
    const element = document.getElementById(id);
    if (!element || typeof echarts === "undefined") return null;
    const chart = echarts.init(element, null, { renderer: "canvas" });
    chart.setOption(option);
    charts.push(chart);
    return chart;
  };

  const globalData = readJSON("global-chart-data");
  if (globalData && Array.isArray(globalData.dates)) {
    const series = (globalData.series || []).map((item, index) => ({
      name: item.name,
      type: "line",
      smooth: 0.32,
      symbol: "circle",
      symbolSize: 5,
      showSymbol: false,
      data: item.data || [],
      lineStyle: { width: 2.4, color: palette[index % palette.length] },
      itemStyle: { color: palette[index % palette.length] },
      areaStyle: {
        opacity: index === 0 ? 0.14 : 0.035,
        color: palette[index % palette.length]
      },
      emphasis: { focus: "series" }
    }));

    makeChart("globalViewsChart", {
      animationDuration: 700,
      color: palette,
      tooltip,
      legend: {
        type: "scroll",
        top: 0,
        right: 0,
        textStyle: { color: muted, fontSize: 11 },
        pageTextStyle: { color: muted }
      },
      grid: { left: 12, right: 12, top: 46, bottom: 4, containLabel: true },
      xAxis: {
        type: "category",
        boundaryGap: false,
        data: globalData.dates,
        axisLine: { lineStyle: { color: grid } },
        axisTick: { show: false },
        axisLabel: { ...axisLabel, formatter: value => String(value).slice(5) }
      },
      yAxis: {
        type: "value",
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { ...axisLabel, formatter: compact },
        splitLine
      },
      series
    });
  }

  const distribution = readJSON("channel-distribution-data");
  if (Array.isArray(distribution)) {
    makeChart("channelMixChart", {
      animationDuration: 700,
      color: palette,
      tooltip: {
        trigger: "item",
        backgroundColor: tooltipBackground,
        borderColor: "rgba(255,255,255,.1)",
        textStyle: { color: text },
        formatter: params => params.name + "<br><b>" + compact(params.value) + "</b> views · " + params.percent + "%"
      },
      legend: {
        bottom: 0,
        left: "center",
        type: "scroll",
        textStyle: { color: muted, fontSize: 11 }
      },
      series: [{
        type: "pie",
        radius: ["58%", "79%"],
        center: ["50%", "44%"],
        avoidLabelOverlap: true,
        padAngle: 3,
        itemStyle: { borderRadius: 8, borderColor: "#0b0e16", borderWidth: 3 },
        label: { show: false },
        emphasis: { scaleSize: 8 },
        data: distribution
      }],
      graphic: [{
        type: "text",
        left: "center",
        top: "39%",
        style: { text: compact(distribution.reduce((sum, item) => sum + Number(item.value || 0), 0)), fill: text, fontSize: 22, fontWeight: 700 }
      }, {
        type: "text",
        left: "center",
        top: "49%",
        style: { text: "Views", fill: muted, fontSize: 11 }
      }]
    });
  }

  const channelData = readJSON("channel-analytics-data");
  if (channelData && Array.isArray(channelData.daily)) {
    const daily = channelData.daily;
    const dates = daily.map(item => item.date);
    const views = daily.map(item => Number(item.views || 0));
    const subscribers = daily.map(item => Number(item.subscribers_net || 0));
    const likes = daily.map(item => Number(item.likes || 0));
    const comments = daily.map(item => Number(item.comments || 0));
    const shares = daily.map(item => Number(item.shares || 0));
    const watch = daily.map(item => Number(item.watch_minutes || 0));
    const avgDuration = daily.map(item => Number(item.average_view_duration || 0));

    makeChart("channelPerformanceChart", {
      animationDuration: 700,
      tooltip,
      legend: { top: 0, right: 0, textStyle: { color: muted }, data: ["Views", "Net Subscribers"] },
      grid: { left: 10, right: 10, top: 44, bottom: 2, containLabel: true },
      xAxis: { type: "category", data: dates, boundaryGap: false, axisTick: { show: false }, axisLine: { lineStyle: { color: grid } }, axisLabel: { ...axisLabel, formatter: v => String(v).slice(5) } },
      yAxis: [
        { type: "value", axisLabel: { ...axisLabel, formatter: compact }, splitLine },
        { type: "value", axisLabel: { ...axisLabel, formatter: v => (v > 0 ? "+" : "") + compact(v) }, splitLine: { show: false } }
      ],
      series: [
        {
          name: "Views", type: "line", smooth: .35, showSymbol: false, data: views,
          lineStyle: { color: "#ff3654", width: 2.8 },
          itemStyle: { color: "#ff3654" },
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: "rgba(255,54,84,.35)" },
              { offset: 1, color: "rgba(255,54,84,0)" }
            ])
          }
        },
        {
          name: "Net Subscribers", type: "bar", yAxisIndex: 1, data: subscribers, barMaxWidth: 10,
          itemStyle: {
            borderRadius: [5,5,2,2],
            color: params => Number(params.value) >= 0 ? "#34d399" : "#fb7185"
          }
        }
      ]
    });

    makeChart("engagementChart", {
      animationDuration: 700,
      color: ["#8b5cf6", "#22d3ee", "#fbbf24"],
      tooltip,
      legend: { top: 0, right: 0, textStyle: { color: muted } },
      grid: { left: 8, right: 8, top: 44, bottom: 2, containLabel: true },
      xAxis: { type: "category", data: dates, axisTick: { show: false }, axisLine: { lineStyle: { color: grid } }, axisLabel: { ...axisLabel, formatter: v => String(v).slice(5) } },
      yAxis: { type: "value", axisLabel: { ...axisLabel, formatter: compact }, splitLine },
      series: [
        { name: "Likes", type: "bar", stack: "engagement", data: likes, barMaxWidth: 14, itemStyle: { borderRadius: [4,4,0,0] } },
        { name: "Comments", type: "bar", stack: "engagement", data: comments, barMaxWidth: 14 },
        { name: "Shares", type: "line", smooth: true, showSymbol: false, data: shares, lineStyle: { width: 2.2 } }
      ]
    });

    makeChart("watchTimeChart", {
      animationDuration: 700,
      tooltip: { ...tooltip, formatter: params => params.map(p => {
        const value = p.seriesName === "Avg Duration" ? duration(p.value) : compact(p.value) + " min";
        return p.marker + p.seriesName + ": <b>" + value + "</b>";
      }).join("<br>") },
      legend: { top: 0, right: 0, textStyle: { color: muted } },
      grid: { left: 8, right: 8, top: 44, bottom: 2, containLabel: true },
      xAxis: { type: "category", data: dates, boundaryGap: false, axisTick: { show: false }, axisLine: { lineStyle: { color: grid } }, axisLabel: { ...axisLabel, formatter: v => String(v).slice(5) } },
      yAxis: [
        { type: "value", axisLabel: { ...axisLabel, formatter: compact }, splitLine },
        { type: "value", axisLabel: { ...axisLabel, formatter: duration }, splitLine: { show: false } }
      ],
      series: [
        {
          name: "Watch Minutes", type: "line", smooth: .35, showSymbol: false, data: watch,
          lineStyle: { color: "#22d3ee", width: 2.5 },
          areaStyle: { color: "rgba(34,211,238,.12)" }
        },
        {
          name: "Avg Duration", type: "line", yAxisIndex: 1, smooth: .3, showSymbol: false, data: avgDuration,
          lineStyle: { color: "#fbbf24", width: 2.2 }
        }
      ]
    });

    const top = (channelData.top_videos || []).slice(0, 8).reverse();
    makeChart("topVideosChart", {
      animationDuration: 700,
      tooltip: {
        trigger: "axis",
        axisPointer: { type: "shadow" },
        backgroundColor: tooltipBackground,
        borderColor: "rgba(255,255,255,.1)",
        textStyle: { color: text },
        formatter: params => {
          const p = params[0];
          const original = top[p.dataIndex] || {};
          return "<b>" + (original.title || "") + "</b><br>Views: " + compact(p.value);
        }
      },
      grid: { left: 5, right: 14, top: 8, bottom: 4, containLabel: true },
      xAxis: { type: "value", axisLabel: { ...axisLabel, formatter: compact }, splitLine },
      yAxis: {
        type: "category",
        data: top.map(item => {
          const title = String(item.title || "");
          return title.length > 24 ? title.slice(0, 24) + "…" : title;
        }),
        axisLine: { show: false },
        axisTick: { show: false },
        axisLabel: { color: muted, width: 150, overflow: "truncate" }
      },
      series: [{
        type: "bar",
        data: top.map(item => Number(item.period_views || 0)),
        barMaxWidth: 16,
        itemStyle: {
          borderRadius: [0, 8, 8, 0],
          color: new echarts.graphic.LinearGradient(1, 0, 0, 0, [
            { offset: 0, color: "#ff3654" },
            { offset: 1, color: "#8b5cf6" }
          ])
        }
      }]
    });
  }

  const openSidebar = () => document.body.classList.add("sidebar-open");
  const closeSidebar = () => document.body.classList.remove("sidebar-open");
  document.querySelectorAll("[data-sidebar-open]").forEach(el => el.addEventListener("click", openSidebar));
  document.querySelectorAll("[data-sidebar-close],[data-sidebar-backdrop]").forEach(el => el.addEventListener("click", closeSidebar));

  if (window.lucide) window.lucide.createIcons({ attrs: { "stroke-width": 1.8 } });

  let resizeFrame = null;
  window.addEventListener("resize", () => {
    if (resizeFrame) cancelAnimationFrame(resizeFrame);
    resizeFrame = requestAnimationFrame(() => charts.forEach(chart => chart.resize()));
  });
})();