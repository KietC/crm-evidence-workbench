const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("okkiDesktop", Object.freeze({
  back: () => ipcRenderer.send("browser:back"),
  forward: () => ipcRenderer.send("browser:forward"),
  reload: () => ipcRenderer.send("browser:reload"),
  home: () => ipcRenderer.send("browser:home"),
  navigate: url => ipcRenderer.send("browser:navigate", url),
  getBrowserState: () => ipcRenderer.invoke("browser:get-state"),
  importCookies: () => ipcRenderer.invoke("session:import-cookies"),
  getLayout: () => ipcRenderer.invoke("layout:get"),
  setSidebarWidth: width => ipcRenderer.send("layout:set-sidebar-width", width),
  onBrowserState: listener => {
    const wrapped = (_event, state) => listener(state);
    ipcRenderer.on("browser:state", wrapped);
    return () => ipcRenderer.removeListener("browser:state", wrapped);
  },
  onLayout: listener => {
    const wrapped = (_event, state) => listener(state);
    ipcRenderer.on("layout:changed", wrapped);
    return () => ipcRenderer.removeListener("layout:changed", wrapped);
  }
}));
