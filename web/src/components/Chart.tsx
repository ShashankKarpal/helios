import ReactECharts from "echarts-for-react";
import type { EChartsOption } from "echarts";

interface Props {
  option: EChartsOption;
  height?: number;
}

// Thin wrapper that applies the Helios dark theme defaults to every chart.
export function Chart({ option, height = 240 }: Props) {
  const themed: EChartsOption = {
    backgroundColor: "transparent",
    textStyle: {
      color: "#8F8C85",
      fontFamily: "system-ui, -apple-system, sans-serif",
    },
    grid: {
      left: 8,
      right: 12,
      top: 24,
      bottom: 4,
      containLabel: true,
      ...(option.grid as object),
    },
    tooltip: {
      trigger: "axis",
      backgroundColor: "#171614",
      borderColor: "#292826",
      borderWidth: 1,
      textStyle: { color: "#F3F1EB", fontSize: 12 },
      ...(option.tooltip as object),
    },
    ...option,
  };

  return (
    <ReactECharts
      option={themed}
      style={{ height, width: "100%" }}
      opts={{ renderer: "canvas" }}
      notMerge
    />
  );
}
