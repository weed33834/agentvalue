<template>
  <!-- 无障碍：雷达图为纯图形信息，提供 role="img" 与文字摘要，让屏幕阅读器可读出各维度得分 -->
  <div v-if="hasData" role="img" :aria-label="chartSummary">
    <v-chart class="radar-chart" :option="option" autoresize />
  </div>
  <el-empty v-else description="暂无维度数据" />
</template>

<script setup>
import { computed } from 'vue'
import VChart from 'vue-echarts'
import '@/utils/echarts'

const props = defineProps({
  dimensions: {
    type: Array,
    default: () => [],
  },
  scores: {
    type: Array,
    default: () => [],
  },
})

const hasData = computed(() => {
  return props.dimensions.length > 0 && props.dimensions.length === props.scores.length
})

// 无障碍：构造雷达图的文字替代描述
const chartSummary = computed(() => {
  const items = props.dimensions.map((name, i) => `${name} ${props.scores[i]}分`).join('；')
  return `能力雷达图，共${props.dimensions.length}个维度：${items}`
})

const option = computed(() => ({
  tooltip: {},
  radar: {
    indicator: props.dimensions.map((name) => ({ name, max: 100 })),
    radius: '65%',
  },
  series: [
    {
      type: 'radar',
      data: [
        {
          value: props.scores,
          name: '能力雷达',
          areaStyle: { color: 'rgba(103, 194, 58, 0.3)' },
          lineStyle: { color: '#67c23a' },
          itemStyle: { color: '#67c23a' },
        },
      ],
    },
  ],
}))
</script>

<style scoped>
.radar-chart {
  width: 100%;
  height: 360px;
}
</style>
