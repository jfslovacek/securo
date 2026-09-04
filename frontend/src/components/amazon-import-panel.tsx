import { useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { toast } from 'sonner'
import { AlertTriangle, CheckCircle2, FileText, Info, Upload, X } from 'lucide-react'

import { accounts as accountsApi, amazon as amazonApi } from '@/lib/api'
import type { AmazonMatchReport } from '@/types'
import { invalidateFinancialQueries } from '@/lib/invalidate-queries'
import { Button } from '@/components/ui/button'
import { Label } from '@/components/ui/label'
import { useWorkspace } from '@/contexts/workspace-context'

const SELECT_CLASS =
  'border border-border rounded-md px-3 py-2 text-sm bg-card focus:outline-none focus-visible:ring-ring/30 focus-visible:ring-[2px]'

/**
 * The purchases half of the import page.
 *
 * The Amazon "Order History" export carries no transactions — it lists what
 * was bought, not what was charged. Uploading it therefore never creates
 * rows: it pairs each purchase with the credit-card charge that paid for it
 * (one-to-one, exact amount, ship-date window) and folds the item names into
 * the matched charge's notes, where rules and the assistant can use them.
 * The same file re-uploaded is a no-op, so users can keep it as a routine
 * "sync Amazon" gesture.
 */
export function AmazonImportPanel() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { canWrite } = useWorkspace()
  const fileInputRef = useRef<HTMLInputElement | null>(null)

  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<AmazonMatchReport | null>(null)
  const [accountId, setAccountId] = useState('')
  const [loading, setLoading] = useState(false)
  const [importing, setImporting] = useState(false)
  const [dragOver, setDragOver] = useState(false)

  const { data: accountsList } = useQuery({
    queryKey: ['accounts'],
    queryFn: () => accountsApi.list(),
  })
  const cardAccounts = (accountsList ?? []).filter((a) => a.type === 'credit_card' && !a.is_closed)

  async function runPreview(selected: File, account: string) {
    setLoading(true)
    try {
      const result = await amazonApi.previewOrders(selected, account || undefined)
      setPreview(result)
    } catch (error: unknown) {
      const detail = (error as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      toast.error(detail || t('amazonImport.previewError'))
      setPreview(null)
    } finally {
      setLoading(false)
    }
  }

  function handleFile(selected: File | null) {
    setFile(selected)
    setPreview(null)
    if (selected) runPreview(selected, accountId)
  }

  function handleReset() {
    handleFile(null)
    if (fileInputRef.current) fileInputRef.current.value = ''
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault()
    setDragOver(false)
    const dropped = e.dataTransfer.files?.[0]
    if (dropped) handleFile(dropped)
  }

  // Re-preview when the target card changes, so the counts on screen always
  // describe the import that would actually run.
  function handleAccountChange(value: string) {
    setAccountId(value)
    if (file) runPreview(file, value)
  }

  async function handleImport() {
    if (!file || !preview) return
    setImporting(true)
    try {
      const result = await amazonApi.importOrders(file, accountId || undefined)
      invalidateFinancialQueries(queryClient)
      toast.success(
        t('amazonImport.imported', { count: result.charges_parsed, linked: result.auto_matched }),
      )
      handleReset()
    } catch {
      toast.error(t('amazonImport.importError'))
    } finally {
      setImporting(false)
    }
  }

  const nothingToImport = !!preview && preview.charges_parsed === 0

  return (
    <div className="space-y-6">
      {canWrite && (
        <div
          className={`cursor-pointer rounded-xl border-2 border-dashed bg-card transition-all ${
            dragOver ? 'border-primary bg-primary/5' : 'border-border hover:border-border'
          }`}
          onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
          onDragLeave={() => setDragOver(false)}
          onDrop={handleDrop}
          onClick={() => !loading && fileInputRef.current?.click()}
        >
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,text/csv"
            className="hidden"
            onChange={(e) => handleFile(e.target.files?.[0] ?? null)}
          />

          <div className="flex flex-col items-center justify-center px-6 py-12 text-center">
            {loading ? (
              <>
                <div className="mb-4 flex h-12 w-12 animate-pulse items-center justify-center rounded-full bg-primary/10">
                  <FileText size={22} className="text-primary" />
                </div>
                <p className="text-sm font-semibold text-foreground">{t('amazonImport.reading')}</p>
                <p className="mt-1 text-xs text-muted-foreground">{file?.name}</p>
              </>
            ) : file && preview && !nothingToImport ? (
              <>
                <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-emerald-100">
                  <CheckCircle2 size={22} className="text-emerald-500" />
                </div>
                <p className="text-sm font-semibold text-foreground">{file.name}</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  {t('amazonImport.summaryCharges', { count: preview.charges_parsed })}
                </p>
                <button
                  className="mt-3 flex items-center gap-1 text-xs text-muted-foreground transition-colors hover:text-rose-500"
                  onClick={(e) => { e.stopPropagation(); handleReset() }}
                >
                  <X size={12} /> {t('import.removeFile')}
                </button>
              </>
            ) : (
              <>
                <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-muted">
                  <Upload size={22} className="text-muted-foreground" />
                </div>
                <p className="mb-1 text-sm font-semibold text-foreground">{t('import.dragOrClick')}</p>
                <p className="text-xs text-muted-foreground">{t('amazonImport.chooseHint')}</p>
              </>
            )}
          </div>
        </div>
      )}

      {preview && !nothingToImport && (
        <div className="overflow-hidden rounded-xl border border-border bg-card shadow-sm">
          <div className="border-b border-border px-4 py-4 sm:px-5">
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
              <span className="font-semibold text-foreground">
                {t('amazonImport.summaryCharges', { count: preview.charges_parsed })}
              </span>
              <span className="text-xs text-muted-foreground">
                {t('amazonImport.summaryMatch', {
                  linked: preview.auto_matched,
                  suggested: preview.suggestions,
                  unmatched: preview.unmatched,
                })}
              </span>
              {preview.skipped_existing > 0 && (
                <span className="text-xs text-muted-foreground">
                  {t('amazonImport.summarySkipped', { count: preview.skipped_existing })}
                </span>
              )}
            </div>
          </div>

          {/* Which card to reconcile against. "All" routes each purchase to the
              card its export row names (last-4), like the backend default. */}
          <div className="border-b border-border bg-muted/50 px-4 py-4 sm:px-5">
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-4">
              <Label htmlFor="amazon-import-account" className="shrink-0 whitespace-nowrap text-sm text-muted-foreground">
                {t('amazonImport.matchTo')}
              </Label>
              <select
                id="amazon-import-account"
                className={`flex-1 ${SELECT_CLASS}`}
                value={accountId}
                onChange={(e) => handleAccountChange(e.target.value)}
              >
                <option value="">{t('amazonImport.autoDetect')}</option>
                {cardAccounts.map((a) => (
                  <option key={a.id} value={a.id}>{a.display_name ?? a.name}</option>
                ))}
              </select>
            </div>
          </div>

          {preview.suggested.length > 0 && (
            <div className="border-b border-border bg-blue-50 px-4 py-3 dark:bg-blue-950 sm:px-5">
              <p className="mb-2 flex items-center gap-2 text-sm font-medium text-blue-700 dark:text-blue-300">
                <Info size={14} />
                {t('amazonImport.suggestedTitle', { count: preview.suggestions })}
              </p>
              <ul className="space-y-1 text-xs text-blue-600 dark:text-blue-300/80">
                {preview.suggested.slice(0, 8).map((s) => (
                  <li key={`${s.order_id}-${s.tracking}`}>
                    {s.order_id} · {s.ship_date} · {s.amount} → {s.transaction_description ?? '—'}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {preview.matches.length > 0 && (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="bg-muted/40 text-xs uppercase text-muted-foreground">
                  <tr>
                    <th className="px-3 py-2 text-left sm:px-5">{t('amazonImport.field.order')}</th>
                    <th className="px-3 py-2 text-left">{t('amazonImport.field.date')}</th>
                    <th className="px-3 py-2 text-right">{t('amazonImport.field.amount')}</th>
                    <th className="px-3 py-2 text-left sm:px-5">{t('amazonImport.field.charge')}</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-border">
                  {preview.matches.slice(0, 50).map((m) => (
                    <tr key={`${m.order_id}-${m.tracking}`}>
                      <td className="px-3 py-1.5 font-medium sm:px-5">{m.order_id}</td>
                      <td className="px-3 py-1.5">{m.ship_date}</td>
                      <td className="px-3 py-1.5 text-right tabular-nums">{m.amount}</td>
                      <td className="px-3 py-1.5 text-muted-foreground sm:px-5">{m.transaction_description}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {preview.matches.length > 50 && (
                <p className="border-t border-border px-4 py-2 text-xs text-muted-foreground sm:px-5">
                  {t('amazonImport.moreRows', { count: preview.matches.length - 50 })}
                </p>
              )}
            </div>
          )}

          <div className="flex items-center justify-between gap-3 border-t border-border px-4 py-4 sm:px-5">
            <span className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <AlertTriangle size={12} />
              {t('amazonImport.matchOnlyHint')}
            </span>
            <div className="flex gap-2">
              <Button variant="outline" onClick={handleReset}>
                <X size={14} className="mr-1" />
                {t('common.cancel')}
              </Button>
              <Button onClick={handleImport} disabled={importing} className="gap-2">
                <Upload size={14} />
                {importing
                  ? t('amazonImport.importing')
                  : t('amazonImport.confirm', { count: preview.auto_matched })}
              </Button>
            </div>
          </div>
        </div>
      )}

      {preview && nothingToImport && (
        <div className="rounded-xl border border-border bg-card px-5 py-4 text-sm text-muted-foreground shadow-sm">
          {t('amazonImport.nothingToImport')}
        </div>
      )}
    </div>
  )
}
