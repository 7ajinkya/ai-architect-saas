<script setup lang="ts">
defineProps<{ label: string }>();
</script>

<template>
  <div class="state state--loading" role="status" aria-live="polite">
    {{ label }}
  </div>
</template>
