<script setup lang="ts">
defineProps<{ label: string }>();
</script>

<template>
  <div class="state state--empty" role="status">
    {{ label }}
  </div>
</template>
