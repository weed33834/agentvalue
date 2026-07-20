// 集中注册各图表组件用到的 echarts 模块，避免每个组件重复 use()
import { use } from 'echarts/core'
import { CanvasRenderer } from 'echarts/renderers'
import { LineChart, RadarChart, BarChart, PieChart, ScatterChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  PolarComponent,
  DataZoomComponent,
  RadarComponent,
} from 'echarts/components'

use([
  CanvasRenderer,
  LineChart,
  RadarChart,
  BarChart,
  PieChart,
  ScatterChart,
  GridComponent,
  TooltipComponent,
  LegendComponent,
  TitleComponent,
  PolarComponent,
  DataZoomComponent,
  RadarComponent,
])
