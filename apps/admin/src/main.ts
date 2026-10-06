// 创建 app：Pinia、Router、i18n、Element Plus、v-permission 指令
import { createApp } from "vue";
import { createPinia } from "pinia";
import ElementPlus from "element-plus";
import "element-plus/dist/index.css";
import "element-plus/theme-chalk/dark/css-vars.css";
import App from "./App.vue";
import { i18n } from "./i18n";
import { router } from "./router";
import { permissionDirective } from "./directives/permission";
import { useThemeStore } from "./store/theme";

const app = createApp(App);
const pinia = createPinia();

app.use(pinia);
app.use(i18n);
app.use(ElementPlus);
app.use(router);
app.directive("permission", permissionDirective);

useThemeStore(pinia).apply();

app.mount("#app");
