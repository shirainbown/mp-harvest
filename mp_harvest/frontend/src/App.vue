<script setup lang="ts">
import { onMounted } from 'vue'
import Sidebar from './layout/Sidebar.vue'
import UpdateModal from './layout/UpdateModal.vue'
import ToastHost from './components/ToastHost.vue'
import CredentialView from './views/CredentialView.vue'
import HistoryView from './views/HistoryView.vue'
import AiModelsView from './views/AiModelsView.vue'
import NetworkView from './views/NetworkView.vue'
import { useUiStore } from './stores/ui'
import { useAccountsStore } from './stores/accounts'
import { useSettingsStore } from './stores/settings'
import { connectWs } from './api/ws'

const ui = useUiStore()

onMounted(() => {
  useAccountsStore().load()
  useSettingsStore().load()
  connectWs()
})
</script>

<template>
  <div class="app">
    <Sidebar />
    <div class="main">
      <CredentialView v-show="ui.view === 'credentials'" />
      <HistoryView v-show="ui.view === 'history'" />
      <AiModelsView v-show="ui.view === 'ai'" />
      <NetworkView v-show="ui.view === 'network'" />
    </div>
  </div>
  <UpdateModal />
  <ToastHost />
</template>
