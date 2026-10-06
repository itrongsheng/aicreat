// 当前项目（顶栏 ProjectSelect）与总后台用户视角 ownerId（顶栏 OwnerSelect），均持久化（docs/02 §3.5、docs/13 §12.2）
import { defineStore } from "pinia";
import { MAX_PAGE_SIZE, type OwnerOption, type Project } from "@aicreat/shared";
import * as projectsApi from "@/api/projects";
import { useAuthStore } from "@/store/auth";

const PROJECT_KEY = "aicreat.project_id";
const OWNER_KEY = "aicreat.owner_id";
/** 顶栏选择器最多加载的项目数（逐页拉取） */
const MAX_PROJECTS = 1000;

function readId(key: string): number {
  try {
    const n = Number(localStorage.getItem(key) || 0);
    return Number.isInteger(n) && n > 0 ? n : 0;
  } catch {
    return 0;
  }
}

function writeId(key: string, value: number): void {
  try {
    if (value > 0) localStorage.setItem(key, String(value));
    else localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

// 并发调用合并为同一次请求（顶栏组件与 Layout 可能同时触发加载）
let projectsPending: Promise<void> | null = null;
let ownersPending: Promise<void> | null = null;
/** 用户视角 / 登录态变化时递增，丢弃变化前发出的请求结果 */
let generation = 0;
/** 登录态变化（resetScope）时递增：负责人候选与用户视角无关，只按账号失效 */
let accountGeneration = 0;

export const useProjectStore = defineStore("project", {
  state: () => ({
    /** 当前项目；0 = 全部项目 / 未选择 */
    currentId: readId(PROJECT_KEY),
    projects: [] as Project[],
    projectsLoaded: false,
    loadingProjects: false,
    /** 总后台的用户视角；0 = 全部用户 */
    ownerId: readId(OWNER_KEY),
    owners: [] as OwnerOption[],
    ownersLoaded: false,
    loadingOwners: false,
  }),
  getters: {
    currentProject: (s): Project | null => s.projects.find((p) => p.id === s.currentId) ?? null,
    currentOwner: (s): OwnerOption | null => s.owners.find((o) => o.id === s.ownerId) ?? null,
    ownerName(): string {
      const owner = this.currentOwner;
      if (owner) return owner.display_name || owner.username;
      return this.ownerId > 0 ? `#${this.ownerId}` : "";
    },
  },
  actions: {
    setCurrent(id: number) {
      this.currentId = id > 0 ? id : 0;
      writeId(PROJECT_KEY, this.currentId);
    },
    /** 加载可见的 active 项目（GET 请求由 client.ts 按 ownerId 附加 owner_id）；持久化的 currentId 不在列表中时重置为 0 */
    load(): Promise<void> {
      if (!projectsPending) {
        projectsPending = this.fetchProjects().finally(() => {
          projectsPending = null;
        });
      }
      return projectsPending;
    },
    async fetchProjects() {
      const auth = useAuthStore();
      if (!auth.token || !auth.hasPermission("content.projects.view")) {
        this.projects = [];
        this.projectsLoaded = true;
        return;
      }
      this.loadingProjects = true;
      const gen = generation;
      try {
        const all: Project[] = [];
        let page = 1;
        for (;;) {
          const res = await projectsApi.list({ status: "active", page, page_size: MAX_PAGE_SIZE });
          all.push(...res.items);
          if (res.items.length < MAX_PAGE_SIZE || all.length >= res.total || all.length >= MAX_PROJECTS) break;
          page += 1;
        }
        if (gen !== generation) return;
        this.projects = all;
        this.projectsLoaded = true;
        if (this.currentId > 0 && !all.some((p) => p.id === this.currentId)) this.setCurrent(0);
      } finally {
        this.loadingProjects = false;
      }
    },
    /** 负责人候选（仅总后台且有 content.projects.view 时请求） */
    loadOwners(force = false): Promise<void> {
      if (!ownersPending) {
        ownersPending = this.fetchOwners(force).finally(() => {
          ownersPending = null;
        });
      }
      return ownersPending;
    },
    async fetchOwners(force: boolean) {
      const auth = useAuthStore();
      if (!auth.token || !auth.isAllScope || !auth.hasPermission("content.projects.view")) {
        this.owners = [];
        if (this.ownerId) this.applyOwner(0);
        return;
      }
      if (this.ownersLoaded && !force) return;
      this.loadingOwners = true;
      const gen = accountGeneration;
      try {
        const owners = await projectsApi.ownerOptions();
        if (gen !== accountGeneration) return;
        this.owners = owners;
        this.ownersLoaded = true;
        if (this.ownerId > 0 && !this.owners.some((o) => o.id === this.ownerId)) this.applyOwner(0);
      } finally {
        this.loadingOwners = false;
      }
    },
    applyOwner(id: number) {
      const next = id > 0 ? id : 0;
      if (next !== this.ownerId) generation += 1;
      this.ownerId = next;
      writeId(OWNER_KEY, this.ownerId);
    },
    /** 切换用户视角：currentId 重置为 0 并重新加载项目列表 */
    async setOwner(id: number) {
      const next = id > 0 ? id : 0;
      if (next === this.ownerId) return;
      this.applyOwner(next);
      this.setCurrent(0);
      if (projectsPending) await projectsPending.catch(() => undefined);
      await this.load();
    },
    /** 登录 / 退出时清空用户视角、当前项目与候选（13 §12.2） */
    resetScope() {
      generation += 1;
      accountGeneration += 1;
      this.ownerId = 0;
      this.currentId = 0;
      this.owners = [];
      this.ownersLoaded = false;
      this.projects = [];
      this.projectsLoaded = false;
      writeId(OWNER_KEY, 0);
      writeId(PROJECT_KEY, 0);
    },
  },
});
