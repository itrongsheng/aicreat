<script setup lang="ts">
// 系统配置（docs/04 §6.6、§7.18）：按配置键分 Tab。system_info 按语言分别维护表单；
// 其它 8 个键以 JsonEditor 编辑，保存 PUT /admin/settings/{key}，后端校验错误逐项展示（后续阶段可替换为专用表单）。
import { computed, onMounted, reactive, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useI18n } from "vue-i18n";
import { ElMessage, type FormInstance, type FormRules } from "element-plus";
import { Refresh } from "@element-plus/icons-vue";
import { SUPPORTED_LOCALES, type Locale, type Setting, type SiteInfo, type ValidationErrorItem } from "@aicreat/shared";
import { validationErrors } from "@/api/client";
import * as settingsApi from "@/api/settings";
import JsonEditor from "@/components/JsonEditor.vue";
import { usePermission } from "@/composables/usePermission";

type JsonKey = Exclude<settingsApi.SettingKey, "system_info">;
type JsonValue = Record<string, unknown>;

const JSON_KEYS: JsonKey[] = [
  "generation_config",
  "media_config",
  "monitoring_config",
  "geo_engines",
  "seo_providers",
  "alert_config",
  "ai_routing_config",
  "stats_config",
];
/** 引用环境变量的字段：GET 时同级附加 configured: true|false，保存前剔除 */
const ENV_FIELDS = ["credential_env", "site_url_env", "url_env"] as const;

const { t } = useI18n();
const { has } = usePermission();
const route = useRoute();
const router = useRouter();

const canUpdate = computed(() => has("system.settings.update"));
const loading = ref(false);

const activeTab = ref<settingsApi.SettingKey>(
  (settingsApi.SETTING_KEYS as readonly string[]).includes(String(route.query.tab)) ? (route.query.tab as settingsApi.SettingKey) : "generation_config",
);
watch(activeTab, (tab) => {
  if (route.query.tab !== tab) void router.replace({ query: { ...route.query, tab } });
});

// ---------- JSON 配置键 ----------
interface JsonState {
  value: JsonValue;
  original: string;
  errors: ValidationErrorItem[];
  saving: boolean;
  valid: boolean;
  loaded: boolean;
}

function emptyState(): JsonState {
  return { value: {}, original: "{}", errors: [], saving: false, valid: true, loaded: false };
}

const states = reactive(Object.fromEntries(JSON_KEYS.map((k) => [k, emptyState()])) as Record<JsonKey, JsonState>);
const editors = ref<Partial<Record<JsonKey, InstanceType<typeof JsonEditor>>>>({});

function setEditorRef(key: JsonKey, el: unknown) {
  if (el) editors.value[key] = el as InstanceType<typeof JsonEditor>;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function applyValue(key: JsonKey, value: unknown) {
  const state = states[key];
  state.value = isPlainObject(value) ? value : {};
  state.original = JSON.stringify(state.value);
  state.errors = [];
  state.valid = true;
  state.loaded = true;
}

function isDirty(key: JsonKey): boolean {
  return JSON.stringify(states[key].value) !== states[key].original;
}

/** 递归剔除与 *_env 字段同级的 configured（只读回显，不属于配置值） */
function stripConfigured(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stripConfigured);
  if (!isPlainObject(value)) return value;
  const hasEnv = ENV_FIELDS.some((f) => f in value);
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(value)) {
    if (hasEnv && k === "configured") continue;
    out[k] = stripConfigured(v);
  }
  return out;
}

interface EnvEntry {
  path: string;
  field: string;
  env: string;
  configured: boolean | null;
}

function collectEnv(value: unknown, path: string[] = [], out: EnvEntry[] = []): EnvEntry[] {
  if (Array.isArray(value)) {
    value.forEach((item, idx) => {
      const label = isPlainObject(item) && typeof item.code === "string" ? item.code : String(idx);
      collectEnv(item, [...path, label], out);
    });
    return out;
  }
  if (!isPlainObject(value)) return out;
  for (const field of ENV_FIELDS) {
    if (field in value) {
      out.push({
        path: path.join("."),
        field,
        env: typeof value[field] === "string" ? (value[field] as string) : "",
        configured: typeof value.configured === "boolean" ? value.configured : null,
      });
    }
  }
  for (const [k, v] of Object.entries(value)) if (isPlainObject(v) || Array.isArray(v)) collectEnv(v, [...path, k], out);
  return out;
}

const envEntries = computed<Record<JsonKey, EnvEntry[]>>(
  () => Object.fromEntries(JSON_KEYS.map((k) => [k, collectEnv(states[k].value)])) as Record<JsonKey, EnvEntry[]>,
);

async function reloadKey(key: JsonKey) {
  try {
    const setting = await settingsApi.getSetting<JsonValue>(key, "*");
    applyValue(key, setting.value);
  } catch {
    /* 拦截器已提示 */
  }
}

async function saveKey(key: JsonKey) {
  const state = states[key];
  if (!editors.value[key]?.validate() || !state.valid) return;
  state.saving = true;
  state.errors = [];
  try {
    const saved = await settingsApi.saveSetting<JsonValue>(key, stripConfigured(state.value) as JsonValue, "*", { silent: true });
    applyValue(key, saved.value);
    ElMessage.success(t("common.saved"));
  } catch (err) {
    const items = validationErrors(err);
    state.errors = items;
    const message = err instanceof Error ? err.message : t("common.requestFailed");
    ElMessage.error(items.length ? `${message}（${items.length}）` : message);
  } finally {
    state.saving = false;
  }
}

// ---------- system_info ----------
const siteLocale = ref<Locale>("zh-CN");
const siteCache = reactive<Partial<Record<Locale, SiteInfo>>>({});
const siteForm = reactive<SiteInfo>({ site_name: "", logo_url: "", footer: "", support_contact: "" });
const siteFormRef = ref<FormInstance>();
const siteSaving = ref(false);
const siteErrors = ref<ValidationErrorItem[]>([]);
const siteFieldErrors = computed<Record<string, string>>(() => {
  const out: Record<string, string> = {};
  for (const item of siteErrors.value) {
    const field = String(item.loc[item.loc.length - 1] ?? "");
    if (field && !out[field]) out[field] = item.msg;
  }
  return out;
});

const URL_RE = /^(https?:\/\/|\/)/i;
const siteRules = computed<FormRules>(() => ({
  site_name: [{ required: true, message: t("settings.siteNameRequired"), trigger: "blur" }],
  logo_url: [
    {
      validator: (_rule, value: string, callback) => (value && !URL_RE.test(value.trim()) ? callback(new Error(t("settings.urlInvalid"))) : callback()),
      trigger: "blur",
    },
  ],
}));

function fillSite(info: Partial<SiteInfo> | undefined) {
  siteForm.site_name = info?.site_name ?? "";
  siteForm.logo_url = info?.logo_url ?? "";
  siteForm.footer = info?.footer ?? "";
  siteForm.support_contact = info?.support_contact ?? "";
  siteErrors.value = [];
  siteFormRef.value?.clearValidate();
}

async function loadSite(locale: Locale, force = false) {
  if (!force && siteCache[locale]) {
    fillSite(siteCache[locale]);
    return;
  }
  try {
    const setting = await settingsApi.getSetting<SiteInfo>("system_info", locale);
    siteCache[locale] = { ...setting.value };
    if (siteLocale.value === locale) fillSite(setting.value);
  } catch {
    /* 拦截器已提示 */
  }
}

watch(siteLocale, (locale) => void loadSite(locale));

async function saveSite() {
  const valid = await siteFormRef.value?.validate().catch(() => false);
  if (!valid) return;
  siteSaving.value = true;
  siteErrors.value = [];
  const locale = siteLocale.value;
  try {
    const value: SiteInfo = {
      site_name: siteForm.site_name.trim(),
      logo_url: siteForm.logo_url.trim(),
      footer: siteForm.footer,
      support_contact: siteForm.support_contact.trim(),
    };
    const saved = await settingsApi.saveSetting<SiteInfo>("system_info", value, locale, { silent: true });
    siteCache[locale] = { ...saved.value };
    fillSite(saved.value);
    ElMessage.success(t("common.saved"));
  } catch (err) {
    siteErrors.value = validationErrors(err);
    ElMessage.error(err instanceof Error ? err.message : t("common.requestFailed"));
  } finally {
    siteSaving.value = false;
  }
}

// ---------- 加载 ----------
async function loadAll() {
  loading.value = true;
  try {
    const list: Setting[] = await settingsApi.listSettings();
    for (const item of list) {
      if ((JSON_KEYS as string[]).includes(item.key) && item.locale === "*") applyValue(item.key as JsonKey, item.value);
      if (item.key === "system_info" && (SUPPORTED_LOCALES as readonly string[]).includes(item.locale)) {
        siteCache[item.locale as Locale] = { ...(item.value as unknown as SiteInfo) };
      }
    }
    // 列表中缺失的键单独读取（与默认值合并后的值）
    await Promise.all(JSON_KEYS.filter((k) => !states[k].loaded).map((k) => reloadKey(k)));
    await loadSite(siteLocale.value);
  } catch {
    /* 拦截器已提示 */
  } finally {
    loading.value = false;
  }
}

onMounted(loadAll);
</script>

<template>
  <el-card v-loading="loading" shadow="never" class="page-card">
    <template #header>
      <div class="page-header">
        <h2 class="page-title">{{ t("settings.title") }}</h2>
        <el-button :icon="Refresh" @click="loadAll">{{ t("common.refresh") }}</el-button>
      </div>
    </template>

    <el-alert v-if="!canUpdate" type="info" :closable="false" show-icon :title="t('settings.readonlyTip')" class="settings-alert" />

    <el-tabs v-model="activeTab">
      <el-tab-pane v-for="key in JSON_KEYS" :key="key" :name="key" :label="t(`settings.tabs.${key}`)" lazy>
        <div class="settings-pane">
          <el-alert v-if="key === 'ai_routing_config'" type="info" :closable="false" show-icon :title="t('settings.aiRoutingHint')" class="settings-alert" />
          <p class="settings-hint">
            <span class="mono">{{ key }}</span>
            · {{ t("settings.jsonHint") }}
          </p>

          <div v-if="envEntries[key].length" class="settings-env">
            <div class="settings-env-title">{{ t("settings.envStatus") }}</div>
            <div class="settings-env-hint">{{ t("settings.envHint") }}</div>
            <el-table :data="envEntries[key]" size="small" border class="settings-env-table">
              <el-table-column prop="path" :label="t('settings.envPath')" min-width="180">
                <template #default="{ row }"><span class="mono">{{ row.path || "-" }}</span></template>
              </el-table-column>
              <el-table-column prop="field" :label="t('settings.envField')" width="150">
                <template #default="{ row }"><span class="mono">{{ row.field }}</span></template>
              </el-table-column>
              <el-table-column prop="env" :label="t('settings.envName')" min-width="180">
                <template #default="{ row }"><span class="mono">{{ row.env || "-" }}</span></template>
              </el-table-column>
              <el-table-column :label="t('common.status')" width="110">
                <template #default="{ row }">
                  <el-tag v-if="row.configured === true" type="success" size="small">{{ t("settings.configured") }}</el-tag>
                  <el-tag v-else-if="row.configured === false" type="warning" size="small">{{ t("settings.notConfigured") }}</el-tag>
                  <span v-else>-</span>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <JsonEditor
            :ref="(el: unknown) => setEditorRef(key, el)"
            v-model="states[key].value"
            object-only
            :rows="22"
            :readonly="!canUpdate"
            :errors="states[key].errors"
            @validity="(v: boolean) => (states[key].valid = v)"
          />

          <div v-if="canUpdate" class="settings-actions">
            <el-button @click="reloadKey(key)">{{ t("common.reload") }}</el-button>
            <el-button type="primary" :loading="states[key].saving" :disabled="!states[key].valid || !isDirty(key)" @click="saveKey(key)">
              {{ t("common.save") }}
            </el-button>
          </div>
        </div>
      </el-tab-pane>

      <el-tab-pane name="system_info" :label="t('settings.tabs.system_info')" lazy>
        <div class="settings-pane">
          <div class="site-locale">
            <span class="site-locale-label">{{ t("settings.locale") }}</span>
            <el-radio-group v-model="siteLocale">
              <el-radio-button value="zh-CN">简体中文（zh-CN）</el-radio-button>
              <el-radio-button value="en-US">English（en-US）</el-radio-button>
            </el-radio-group>
          </div>
          <el-form ref="siteFormRef" :model="siteForm" :rules="siteRules" label-width="110px" :disabled="!canUpdate" class="site-form" @submit.prevent="saveSite">
            <el-form-item :label="t('settings.siteName')" prop="site_name" :error="siteFieldErrors.site_name">
              <el-input v-model="siteForm.site_name" maxlength="100" show-word-limit />
            </el-form-item>
            <el-form-item :label="t('settings.logoUrl')" prop="logo_url" :error="siteFieldErrors.logo_url">
              <el-input v-model="siteForm.logo_url" placeholder="https://" />
              <img v-if="siteForm.logo_url" :src="siteForm.logo_url" alt="" class="site-logo-preview" />
            </el-form-item>
            <el-form-item :label="t('settings.footer')" prop="footer" :error="siteFieldErrors.footer">
              <el-input v-model="siteForm.footer" type="textarea" :rows="3" />
            </el-form-item>
            <el-form-item :label="t('settings.supportContact')" prop="support_contact" :error="siteFieldErrors.support_contact">
              <el-input v-model="siteForm.support_contact" />
            </el-form-item>
            <el-form-item v-if="siteErrors.length && !Object.keys(siteFieldErrors).length">
              <el-alert type="error" :closable="false" :title="siteErrors.map((e) => e.msg).join('；')" />
            </el-form-item>
            <el-form-item v-if="canUpdate">
              <el-button @click="loadSite(siteLocale, true)">{{ t("common.reload") }}</el-button>
              <el-button type="primary" :loading="siteSaving" @click="saveSite">{{ t("common.save") }}</el-button>
            </el-form-item>
          </el-form>
        </div>
      </el-tab-pane>
    </el-tabs>
  </el-card>
</template>

<style scoped>
.settings-pane {
  max-width: 980px;
}
.settings-alert {
  margin-bottom: 12px;
}
.settings-hint {
  margin: 0 0 12px;
  color: var(--el-text-color-secondary);
  font-size: 13px;
}
.settings-env {
  margin-bottom: 12px;
}
.settings-env-title {
  font-weight: 600;
  margin-bottom: 2px;
}
.settings-env-hint {
  color: var(--el-text-color-secondary);
  font-size: 12px;
  margin-bottom: 6px;
}
.settings-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 12px;
}
.site-form {
  max-width: 680px;
}
.site-locale {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 18px;
}
.site-locale-label {
  width: 98px;
  text-align: right;
  color: var(--el-text-color-regular);
  font-size: 14px;
}
.site-logo-preview {
  max-height: 40px;
  max-width: 160px;
  margin-top: 8px;
  display: block;
}
</style>
