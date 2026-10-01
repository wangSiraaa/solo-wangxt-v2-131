const BASE = ''

async function request(path, options = {}) {
  const response = await fetch(BASE + path, {
    headers: options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' },
    ...options
  })
  const text = await response.text()
  const data = text ? JSON.parse(text) : null
  if (!response.ok) throw new Error(JSON.stringify(data ?? response.statusText))
  return data
}

export const api = {
  health: () => request('/health'),
  manifests: () => request('/manifests'),
  manifest: (id) => request(`/manifests/${id}`),
  chunks: (id) => request(`/manifests/${id}/chunks`),
  issues: (id) => request(`/manifests/${id}/issues`),
  finalize: (id) => request(`/manifests/${id}/finalize`, { method: 'POST' }),
  preview: (id, calibrationVersionId) => {
    const suffix = calibrationVersionId ? `?calibration_version_id=${encodeURIComponent(calibrationVersionId)}` : ''
    return request(`/manifests/${id}/preview${suffix}`)
  },
  calibrations: (channelSetHash) =>
    request(`/calibrations${channelSetHash ? `?channel_set_hash=${encodeURIComponent(channelSetHash)}` : ''}`),
  tasks: (manifestId) => request(`/analysis-tasks?manifest_id=${encodeURIComponent(manifestId)}`),
  createTask: (manifestId, calibrationVersionId) =>
    request('/analysis-tasks', {
      method: 'POST',
      body: JSON.stringify({ manifest_id: manifestId, calibration_version_id: calibrationVersionId })
    }),
  runTask: (id) => request(`/analysis-tasks/${id}/run`, { method: 'POST' }),
  cancelTask: (id) => request(`/analysis-tasks/${id}/cancel`, { method: 'POST' }),
  retryTask: (id) => request(`/analysis-tasks/${id}/retry`, { method: 'POST' }),
  reports: (manifestId) => request(`/reports?manifest_id=${encodeURIComponent(manifestId)}`),
  report: (id) => request(`/reports/${id}`)
}
