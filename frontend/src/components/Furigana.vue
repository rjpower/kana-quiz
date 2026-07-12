<script setup lang="ts">
// Renders a furigana-annotated Japanese string: kanji runs get a small
// reading floated above via native <ruby>/<rt>, kana and punctuation pass
// through as plain text. The segments are computed server-side (see
// backend furigana.py) so this component is purely presentational.
//
// The single <span> root keeps the whole annotated phrase as one inline
// box — important inside the cloze sentence's flex row, where each child is
// a flex item: emitting the segments as separate top-level nodes would let
// the row's gap space them out and shatter the sentence.

import type { RubySegment } from '../stores/session'

defineProps<{ segments: RubySegment[] }>()
</script>

<template>
  <span class="furigana"
    ><template v-for="(seg, i) in segments" :key="i"
      ><ruby v-if="seg.r">{{ seg.t }}<rt>{{ seg.r }}</rt></ruby
      ><template v-else>{{ seg.t }}</template></template
  ></span>
</template>

<style scoped>
/* Reserve vertical room so the floated reading doesn't collide with the
   line above when the sentence wraps. */
.furigana {
  line-height: 2.1;
}
.furigana rt {
  font-size: 0.52em;
  font-weight: 400;
  /* Slightly dimmer than the body so the reading supports rather than
     competes with the sentence the learner is parsing. */
  color: var(--muted, #8a93a3);
  user-select: none;
  -webkit-user-select: none;
}
</style>
