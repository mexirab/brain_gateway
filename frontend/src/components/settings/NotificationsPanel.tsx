'use client';

import { useCallback, useEffect, useState } from 'react';
import { Bell, BellOff, Loader2, Send } from 'lucide-react';
import { api } from '@/lib/api';
import type { WebPushInfo } from '@/lib/types';

type DeviceState = 'loading' | 'unsupported' | 'needs-install' | 'blocked' | 'off' | 'on';

function urlBase64ToBuffer(b64url: string): ArrayBuffer {
  const padded = (b64url + '='.repeat((4 - (b64url.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out.buffer;
}

function isIOS(): boolean {
  return /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function isStandalone(): boolean {
  return (
    window.matchMedia('(display-mode: standalone)').matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

function pushSupported(): boolean {
  return 'serviceWorker' in navigator && 'PushManager' in window && 'Notification' in window;
}

/** The worker's registration, or null if /sw.js never registered.
 *  (`serviceWorker.ready` would hang forever in that case.) */
async function registration(timeoutMs = 10_000): Promise<ServiceWorkerRegistration | null> {
  const reg = await navigator.serviceWorker.getRegistration('/');
  if (!reg) return null;
  if (reg.active) return reg;
  return Promise.race([
    navigator.serviceWorker.ready,
    new Promise<null>((resolve) => setTimeout(() => resolve(null), timeoutMs)),
  ]);
}

function sameKey(sub: PushSubscription, publicKey: string): boolean {
  const key = sub.options?.applicationServerKey;
  if (!key) return true; // browser doesn't expose it — assume it matches
  const b64 = btoa(String.fromCharCode(...new Uint8Array(key)))
    .replace(/\+/g, '-')
    .replace(/\//g, '_')
    .replace(/=+$/, '');
  return b64 === publicKey;
}

/**
 * Settings → Notifications: turn on lockscreen notifications for reminders
 * and self-care nudges on THIS device (Web Push, self-hosted sender).
 * Tapping a notification opens Chat, where the Done / Snooze buttons are.
 */
export default function NotificationsPanel() {
  const [server, setServer] = useState<WebPushInfo | null>(null);
  const [device, setDevice] = useState<DeviceState>('loading');
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setError(null);
    let info: WebPushInfo | null = null;
    try {
      info = await api.webPushInfo();
      setServer(info);
    } catch {
      setError('Couldn’t reach Jess to check notification settings.');
    }
    if (!pushSupported()) {
      setDevice(isIOS() && !isStandalone() ? 'needs-install' : 'unsupported');
      return;
    }
    if (Notification.permission === 'denied') {
      setDevice('blocked');
      return;
    }
    try {
      const reg = await registration();
      if (!reg) {
        setDevice('unsupported');
        return;
      }
      const sub = await reg.pushManager.getSubscription();
      if (sub && info?.enabled && info.public_key && sameKey(sub, info.public_key)) {
        // Re-send it (an upsert): the server may have dropped it — a 410
        // prune, a restored database — while this browser still holds it.
        const res = await api.webPushSubscribe(sub.toJSON()).catch(() => null);
        if (res) setServer((cur) => (cur ? { ...cur, devices: res.devices } : cur));
        setDevice('on');
      } else {
        setDevice('off'); // none, or made with an old key — "Turn on" redoes it
      }
    } catch {
      setDevice('off');
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function enable() {
    if (!server?.public_key) {
      setError('Jess’s notification key isn’t available — check the orchestrator log.');
      return;
    }
    setBusy(true);
    setMsg(null);
    setError(null);
    try {
      // iOS only shows the prompt when this runs straight from the tap.
      const permission = await Notification.requestPermission();
      if (permission !== 'granted') {
        setDevice(permission === 'denied' ? 'blocked' : 'off');
        return;
      }
      const reg = await registration();
      if (!reg) throw new Error('no service worker');
      let sub = await reg.pushManager.getSubscription();
      if (sub && !sameKey(sub, server.public_key)) {
        await sub.unsubscribe(); // made for an old key; push would be rejected
        sub = null;
      }
      sub ??= await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToBuffer(server.public_key),
      });
      const res = await api.webPushSubscribe(sub.toJSON());
      setServer((s) => (s ? { ...s, devices: res.devices } : s));
      setDevice('on');
      setMsg('Notifications are on for this device. Try “Send test”.');
    } catch {
      setError('Couldn’t turn on notifications. Check that the dashboard is open over https.');
    } finally {
      setBusy(false);
    }
  }

  async function disable() {
    setBusy(true);
    setMsg(null);
    setError(null);
    try {
      const sub = await (await registration())?.pushManager.getSubscription();
      if (sub) {
        await api.webPushUnsubscribe(sub.endpoint).catch(() => undefined);
        await sub.unsubscribe();
      }
      setDevice('off');
      setMsg('Notifications are off for this device.');
      void refresh();
    } catch {
      setError('Couldn’t turn notifications off.');
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    setMsg(null);
    setError(null);
    try {
      const res = await api.webPushTest();
      setMsg(res.sent ? `Sent to ${res.sent} device${res.sent === 1 ? '' : 's'}.` : 'Nothing was delivered.');
      if (res.failed) setError(`${res.failed} device${res.failed === 1 ? '' : 's'} didn’t accept it.`);
    } catch {
      setError('Couldn’t send a test notification.');
    } finally {
      setBusy(false);
    }
  }

  const btn =
    'inline-flex items-center gap-2 min-h-11 px-4 rounded-lg text-sm font-medium transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-50';

  return (
    <div className="space-y-5">
      <div>
        <h2 className="text-lg font-semibold text-content-primary">Notifications</h2>
        <p className="text-sm text-content-secondary mt-1">
          Get reminders and self-care nudges on this device’s lock screen. Tap one to open Chat, where the Done and
          Snooze buttons are. Jess sends these herself; Apple or Google only pass along an encrypted message they
          can’t read.
        </p>
      </div>

      {device === 'loading' || server === null ? (
        error ? (
          <div className="flex items-center gap-3">
            <p className="text-sm text-danger" role="alert">{error}</p>
            <button
              type="button"
              onClick={() => void refresh()}
              className={`${btn} bg-surface-overlay/40 text-content-secondary hover:text-content-primary`}
            >
              Retry
            </button>
          </div>
        ) : (
          <Loader2 className="animate-spin text-content-muted" size={20} aria-label="Loading" />
        )
      ) : !server.enabled ? (
        <p className="text-sm text-content-secondary">
          Notifications are turned off on the server. Set <code>WEBPUSH_ENABLED=true</code> and{' '}
          <code>WEBPUSH_SUBJECT</code> in <code>.env</code>, then recreate the orchestrator.
        </p>
      ) : device === 'needs-install' ? (
        <ol className="text-sm text-content-secondary list-decimal pl-5 space-y-1">
          <li>In Safari, tap the Share button, then <strong>Add to Home Screen</strong>.</li>
          <li>Open Jess from the new home-screen icon.</li>
          <li>Come back to Settings → Notifications and turn them on.</li>
        </ol>
      ) : device === 'unsupported' ? (
        <p className="text-sm text-content-secondary">
          This browser can’t receive web notifications here. Open the dashboard over https (not a plain-http LAN
          address) and reload.
        </p>
      ) : device === 'blocked' ? (
        <p className="text-sm text-content-secondary">
          Notifications are blocked for this site. Allow them in the device’s settings for Jess, then reload.
        </p>
      ) : (
        <div className="flex flex-wrap gap-2">
          {device === 'off' ? (
            <button type="button" onClick={enable} disabled={busy} className={`${btn} bg-brand-500 text-white hover:bg-brand-600`}>
              {busy ? <Loader2 size={16} className="animate-spin" aria-hidden /> : <Bell size={16} aria-hidden />}
              Turn on for this device
            </button>
          ) : (
            <>
              <button type="button" onClick={test} disabled={busy} className={`${btn} bg-brand-500 text-white hover:bg-brand-600`}>
                {busy ? <Loader2 size={16} className="animate-spin" aria-hidden /> : <Send size={16} aria-hidden />}
                Send test
              </button>
              <button
                type="button"
                onClick={disable}
                disabled={busy}
                className={`${btn} bg-surface-overlay/40 text-content-secondary hover:text-content-primary`}
              >
                <BellOff size={16} aria-hidden />
                Turn off for this device
              </button>
            </>
          )}
        </div>
      )}

      {server?.enabled && (
        <p className="text-xs text-content-muted">
          {server.devices} device{server.devices === 1 ? '' : 's'} signed up.
        </p>
      )}
      <div role="status" aria-live="polite" className="text-sm">
        {msg && <p className="text-success">{msg}</p>}
        {error && device !== 'loading' && server !== null && <p className="text-danger">{error}</p>}
      </div>
    </div>
  );
}
