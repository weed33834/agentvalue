<template>
  <span class="brand-logo">
    <span
      class="brand-logo__mark"
      :style="markStyle"
      aria-hidden="true"
    >
      <svg
        viewBox="0 0 32 32"
        :width="size"
        :height="size"
        focusable="false"
        aria-hidden="true"
      >
        <path
          fill="#ffffff"
          fill-rule="evenodd"
          d="M16 4.5 L27 28 H20.5 L19 22.5 H13 L11.5 28 H5 Z M14.6 20 H17.4 L16 11 Z"
        />
      </svg>
    </span>
    <span
      v-if="showText"
      class="brand-logo__text"
      :style="textStyle"
    >{{ wordmark }}</span>
  </span>
</template>

<script setup>
import { computed } from 'vue'

const props = defineProps({
  // 图标尺寸（px）
  size: { type: Number, default: 28 },
  // 是否显示文字
  showText: { type: Boolean, default: true },
  // 文字字号（px）
  textSize: { type: Number, default: 16 },
  // 品牌文字
  wordmark: { type: String, default: 'AgentValue' },
})

const markStyle = computed(() => ({
  width: `${props.size}px`,
  height: `${props.size}px`,
}))

const textStyle = computed(() => ({
  fontSize: `${props.textSize}px`,
}))
</script>

<style scoped>
.brand-logo {
  display: inline-flex;
  align-items: center;
  gap: 10px;
  line-height: 1;
  color: inherit;
}
.brand-logo__mark {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  border-radius: 8px;
  background: linear-gradient(135deg, #2563eb 0%, #7c3aed 100%);
  box-shadow: 0 2px 8px rgba(37, 99, 235, 0.28);
  flex-shrink: 0;
}
.brand-logo__text {
  font-weight: 700;
  letter-spacing: 0.3px;
  color: currentColor;
  white-space: nowrap;
}
</style>
