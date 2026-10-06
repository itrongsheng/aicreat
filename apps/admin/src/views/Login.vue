<script setup lang="ts">
// 登录页（docs/07 §8.2）：先按当前语言请求 site-info；401 / 403 显示后端 message；429 倒计时禁用按钮
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import type { FormInstance, FormRules } from "element-plus";
import { Lock, User } from "@element-plus/icons-vue";
import type { Locale, SiteInfo } from "@aicreat/shared";
import * as authApi from "@/api/auth";
import { isApiError } from "@/api/client";
import LangSwitch from "@/components/LangSwitch.vue";
import ThemeSwitch from "@/components/ThemeSwitch.vue";
import { resolveHomePath } from "@/router";
import { useAuthStore } from "@/store/auth";

const auth = useAuthStore();
const route = useRoute();
const router = useRouter();
const { t, locale } = useI18n();

const site = ref<SiteInfo | null>(null);
const siteName = computed(() => site.value?.site_name || t("common.appName"));

async function loadSiteInfo() {
  try {
    site.value = await authApi.siteInfo(locale.value as Locale);
  } catch {
    site.value = null; // 失败时使用 i18n 默认文案，不阻塞登录
  }
}

onMounted(loadSiteInfo);
watch(locale, loadSiteInfo);
watch(siteName, (name) => (document.title = `${t("auth.loginTitle")} - ${name}`), { immediate: true });

const formRef = ref<FormInstance>();
const form = reactive({ username: "", password: "" });
const rules = computed<FormRules>(() => ({
  username: [{ required: true, message: t("auth.usernameRequired"), trigger: "blur" }],
  password: [{ required: true, message: t("auth.passwordRequired"), trigger: "blur" }],
}));

const submitting = ref(false);
const errorMessage = ref("");
const retryLeft = ref(0);
let countdown: ReturnType<typeof setInterval> | null = null;

function stopCountdown() {
  if (countdown) clearInterval(countdown);
  countdown = null;
}

function startCountdown(seconds: number) {
  stopCountdown();
  retryLeft.value = Math.max(1, Math.ceil(seconds));
  countdown = setInterval(() => {
    retryLeft.value -= 1;
    if (retryLeft.value <= 0) {
      retryLeft.value = 0;
      errorMessage.value = "";
      stopCountdown();
    }
  }, 1000);
}

onBeforeUnmount(stopCountdown);

const lockedMessage = computed(() => (retryLeft.value > 0 ? t("auth.retryAfter", { seconds: retryLeft.value }) : ""));

function redirectTarget(): string {
  const redirect = route.query.redirect;
  return typeof redirect === "string" && redirect.startsWith("/") && !redirect.startsWith("//") && !redirect.startsWith("/login")
    ? redirect
    : resolveHomePath();
}

async function submit() {
  if (retryLeft.value > 0 || submitting.value) return;
  const valid = await formRef.value?.validate().catch(() => false);
  if (!valid) return;
  submitting.value = true;
  errorMessage.value = "";
  try {
    await auth.login({ username: form.username.trim(), password: form.password });
    await router.replace(redirectTarget());
  } catch (err) {
    if (isApiError(err)) {
      if (err.status === 429 || err.code === 429) {
        const data = err.data as { retry_after?: number } | null;
        startCountdown(Number(data?.retry_after) || 60);
        errorMessage.value = "";
      } else if (err.status === 0) {
        errorMessage.value = t("common.networkError");
      } else {
        errorMessage.value = err.message || t("common.requestFailed");
      }
    } else {
      errorMessage.value = t("common.requestFailed");
    }
    form.password = "";
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <div class="login-page">
    <div class="login-tools">
      <LangSwitch />
      <ThemeSwitch />
    </div>
    <div class="login-panel">
      <div class="login-brand">
        <img v-if="site?.logo_url" :src="site.logo_url" alt="" class="login-logo" />
        <h1 class="login-title">{{ siteName }}</h1>
        <p class="login-subtitle">{{ t("auth.welcome") }}</p>
      </div>
      <el-form ref="formRef" :model="form" :rules="rules" size="large" @submit.prevent="submit">
        <el-form-item prop="username">
          <el-input v-model="form.username" :prefix-icon="User" :placeholder="t('auth.username')" autocomplete="username" autofocus />
        </el-form-item>
        <el-form-item prop="password">
          <el-input
            v-model="form.password"
            :prefix-icon="Lock"
            type="password"
            show-password
            :placeholder="t('auth.password')"
            autocomplete="current-password"
          />
        </el-form-item>
        <el-alert v-if="lockedMessage || errorMessage" :title="lockedMessage || errorMessage" type="error" :closable="false" show-icon class="login-error" />
        <el-button type="primary" native-type="submit" class="login-submit" :loading="submitting" :disabled="retryLeft > 0">
          {{ retryLeft > 0 ? `${t("auth.login")} (${retryLeft}s)` : t("auth.login") }}
        </el-button>
      </el-form>
      <div v-if="site?.support_contact || site?.footer" class="login-footer">
        <div v-if="site?.support_contact">{{ t("auth.contact") }}：{{ site.support_contact }}</div>
        <div v-if="site?.footer">{{ site.footer }}</div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.login-page {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  min-height: 100vh;
  padding: 24px;
  box-sizing: border-box;
  background:
    radial-gradient(circle at 20% 20%, var(--el-color-primary-light-8), transparent 45%),
    radial-gradient(circle at 80% 80%, var(--el-color-primary-light-9), transparent 40%),
    var(--el-bg-color-page);
}
.login-tools {
  position: absolute;
  top: 16px;
  right: 20px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.login-panel {
  width: 380px;
  max-width: 100%;
  padding: 36px 32px 28px;
  border-radius: 12px;
  background: var(--el-bg-color);
  box-shadow: var(--el-box-shadow-light);
}
.login-brand {
  text-align: center;
  margin-bottom: 24px;
}
.login-logo {
  max-height: 48px;
  max-width: 160px;
  margin-bottom: 8px;
}
.login-title {
  margin: 0;
  font-size: 22px;
  font-weight: 600;
  color: var(--el-text-color-primary);
}
.login-subtitle {
  margin: 6px 0 0;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.login-error {
  margin-bottom: 16px;
}
.login-submit {
  width: 100%;
}
.login-footer {
  margin-top: 20px;
  text-align: center;
  color: var(--el-text-color-secondary);
  font-size: 12px;
  line-height: 1.8;
  white-space: pre-line;
}
</style>
