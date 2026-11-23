<script setup lang="ts">
defineProps<{ label: string; detail?: string }>();
</script>

<template>
  <div class="state state--error" role="alert">
    <strong>{{ label }}</strong>
    <span v-if="detail" class="state__detail">{{ detail }}</span>
  </div>
</template>
