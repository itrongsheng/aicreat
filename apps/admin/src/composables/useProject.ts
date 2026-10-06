// 当前项目 id 与切换；生成类页面无当前项目时提示先选择项目
import { computed } from "vue";
import { ElMessage } from "element-plus";
import { t } from "@/i18n";
import { useProjectStore } from "@/store/project";

export function useProject() {
  const store = useProjectStore();
  const projectId = computed(() => store.currentId);
  const project = computed(() => store.currentProject);

  function setProject(id: number) {
    store.setCurrent(id);
  }

  /** 有当前项目时返回 true；否则提示「请先在顶栏选择项目」并返回 false */
  function requireProject(): boolean {
    if (store.currentId > 0) return true;
    ElMessage.warning(t("common.selectProjectFirst"));
    return false;
  }

  return { projectId, project, setProject, requireProject, store };
}
