const jsonHeaders = { 'Content-Type': 'application/json' }

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    let detail = response.statusText
    try { detail = (await response.json()).detail ?? detail } catch {}
    throw new Error(detail)
  }
  return response.json()
}

export const api = {
  manifests: () => request<any[]>('/manifests'),
  manifest: (key: string) => request<any>(`/manifests/${key}`),
  blocks: (key: string) => request<any[]>(`/manifests/${key}/blocks`),
  complete: (key: string) => request<any>(`/manifests/${key}/complete`, { method: 'POST' }),
  calibrations: () => request<any[]>('/calibrations'),
  tasks: () => request<any[]>('/analyses'),
  task: (key: string) => request<any>(`/analyses/by-key/${key}`),
  report: (key: string) => request<any>(`/analyses/by-key/${key}/report`),
  createAnalysis: (body: unknown) => request<any>('/analyses', { method: 'POST', headers: jsonHeaders, body: JSON.stringify(body) }),
  cancel: (key: string) => request<any>(`/analyses/by-key/${key}/cancel`, { method: 'POST' }),
  retry: (key: string) => request<any>(`/analyses/by-key/${key}/retry`, { method: 'POST' })
}
