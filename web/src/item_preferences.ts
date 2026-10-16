import type { Item, ItemPreferencePatch } from './types'

const fields = ['is_favorite', 'favorite_group_ids', 'user_score'] as const
type Field = typeof fields[number]

export function createItemPreferenceTracker() {
  let revision = 0
  const changes = new Map<string, {patch: ItemPreferencePatch; revisions: Partial<Record<Field, number>>}>()
  return {
    record(patch: ItemPreferencePatch) {
      const previous = changes.get(patch.id) || {patch: {id: patch.id}, revisions: {}}
      const revisions = {...previous.revisions}
      for (const field of fields) if (Object.hasOwn(patch, field)) revisions[field] = ++revision
      changes.set(patch.id, {patch: {...previous.patch, ...patch}, revisions})
    },
    snapshot() {
      const startedAt = revision
      return (item: Item): Item => {
        const change = changes.get(item.id)
        if (!change) return item
        const newer: ItemPreferencePatch = {id: item.id}
        for (const field of fields) {
          // Only a local save after this request began can override its response.
          // Later requests remain free to receive changes from another device.
          if ((change.revisions[field] || 0) > startedAt) Object.assign(newer, {[field]: change.patch[field]})
        }
        return {...item, ...newer}
      }
    },
  }
}
