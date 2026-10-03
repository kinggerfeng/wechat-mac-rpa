import { defineStore } from "pinia";
import { ref } from "vue";

import { api, ApiError } from "@shared/api/client";

/**
 * Backend liveness, and nothing else.
 *
 * The three ops pages that need this used to reach into the desktop app's
 * `engine` store, which is about the RPA machine: bot process, log tail,
 * screen-recording and accessibility permissions. None of that exists for an
 * operator reviewing ticks — they never drive the machine, so a permission
 * they can never grant is noise, and importing the engine store to learn
 * "is the server up" is what put engine concerns in the console in the first
 * place.
 *
 * `online` starts as `null`, not `false`. "We asked and it was not there" and
 * "we have not asked yet" are different states, and a page opened directly at
 * a sub-route must not show "离线" for the length of one round trip while the
 * API is answering 200.
 */
export const useConnectionStore = defineStore("connection", () => {
  const online = ref<boolean | null>(null);
  const lastError = ref("");
  const lastUpdated = ref("");

  async function refresh(): Promise<void> {
    try {
      await api.status();
      online.value = true;
      lastError.value = "";
      lastUpdated.value = new Date().toLocaleTimeString("zh-CN", { hour12: false });
    } catch (reason) {
      online.value = false;
      lastError.value =
        reason instanceof ApiError ? reason.message : String(reason);
    }
  }

  return { online, lastError, lastUpdated, refresh };
});
