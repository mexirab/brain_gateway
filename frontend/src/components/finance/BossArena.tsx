'use client';

import { useState } from 'react';
import { Swords, Shield, Loader2, TrendingUp, Wallet } from 'lucide-react';
import { formatCurrency } from '@/lib/finance-utils';

interface Props {
  type: 'bonus' | 'espp';
  defaultInvestPercent: number;
  onDefeatBoss: (data: {
    type: 'bonus' | 'espp';
    amount: number;
    invest_percent: number;
  }) => Promise<void>;
}

export default function BossArena({ type, defaultInvestPercent, onDefeatBoss }: Props) {
  const [amount, setAmount] = useState('');
  const [investPercent, setInvestPercent] = useState(defaultInvestPercent);
  const [busy, setBusy] = useState(false);
  const [defeated, setDefeated] = useState(false);

  const amountNum = parseFloat(amount) || 0;
  const investAmount = amountNum * (investPercent / 100);
  const spendAmount = amountNum - investAmount;

  const isBoss = type === 'bonus' ? 'Bonus' : 'ESPP';
  const bossColor = type === 'bonus' ? 'amber' : 'purple';

  async function handleDefeat() {
    if (amountNum <= 0) return;
    setBusy(true);
    try {
      await onDefeatBoss({ type, amount: amountNum, invest_percent: investPercent });
      setDefeated(true);
    } finally {
      setBusy(false);
    }
  }

  if (defeated) {
    return (
      <div className="glass p-8 border border-amber-500/40 text-center">
        <div className="text-5xl mb-3">⚔️</div>
        <h2 className="text-xl font-bold text-amber-400">Boss Defeated!</h2>
        <p className="text-sm text-zinc-400 mt-2">
          {isBoss} windfall of {formatCurrency(amountNum)} split successfully
        </p>
        <div className="flex justify-center gap-6 mt-4">
          <div>
            <p className="text-lg font-bold text-emerald-400">{formatCurrency(investAmount)}</p>
            <p className="text-xs text-zinc-500">Invested</p>
          </div>
          <div>
            <p className="text-lg font-bold text-brand-400">{formatCurrency(spendAmount)}</p>
            <p className="text-xs text-zinc-500">Guilt-Free Spend</p>
          </div>
        </div>
        <p className="text-sm text-amber-400/80 mt-4 font-medium">+200 XP earned!</p>
      </div>
    );
  }

  return (
    <div className={`glass p-6 border border-${bossColor}-500/30`}>
      {/* Boss header */}
      <div className="flex items-center gap-3 mb-6">
        <div className={`p-3 rounded-xl bg-${bossColor}-500/15`}>
          <Swords size={28} className={`text-${bossColor}-400`} />
        </div>
        <div>
          <h2 className="text-lg font-bold text-zinc-100">
            {isBoss} Boss Battle
          </h2>
          <p className="text-sm text-zinc-500">
            {type === 'bonus'
              ? 'Split your bonus wisely to defeat the boss'
              : 'ESPP windfall — recommended 67% invest / 33% spend'}
          </p>
        </div>
      </div>

      {/* Amount input */}
      <div className="mb-5">
        <label className="block text-xs text-zinc-400 mb-1.5">
          Windfall Amount
        </label>
        <div className="relative">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-zinc-500">$</span>
          <input
            type="number"
            placeholder={type === 'bonus' ? '8231' : '8237'}
            value={amount}
            onChange={(e) => setAmount(e.target.value)}
            className="w-full bg-zinc-800 text-zinc-200 text-lg rounded-lg pl-7 pr-3 py-3 border border-zinc-700 focus:border-brand-500 focus:outline-none font-mono"
            min="0"
            step="1"
          />
        </div>
      </div>

      {/* Invest slider */}
      <div className="mb-5">
        <div className="flex justify-between text-xs text-zinc-400 mb-2">
          <span className="flex items-center gap-1">
            <TrendingUp size={12} />
            Invest: {investPercent}%
          </span>
          <span className="flex items-center gap-1">
            <Wallet size={12} />
            Spend: {100 - investPercent}%
          </span>
        </div>
        <input
          type="range"
          min="0"
          max="100"
          step="1"
          value={investPercent}
          onChange={(e) => setInvestPercent(parseInt(e.target.value))}
          className="w-full accent-brand-500 h-2"
        />

        {/* Split preview */}
        {amountNum > 0 && (
          <div className="flex justify-between mt-3">
            <div className="text-center flex-1">
              <p className="text-lg font-bold text-emerald-400 font-mono">
                {formatCurrency(investAmount)}
              </p>
              <p className="text-xs text-zinc-500">To investments</p>
            </div>
            <div className="w-px bg-zinc-700 mx-3" />
            <div className="text-center flex-1">
              <p className="text-lg font-bold text-brand-400 font-mono">
                {formatCurrency(spendAmount)}
              </p>
              <p className="text-xs text-zinc-500">Guilt-free spend</p>
            </div>
          </div>
        )}
      </div>

      {/* Quick-set buttons */}
      <div className="flex gap-2 mb-5">
        {[
          { label: '50/50', pct: 50 },
          { label: '67/33', pct: 67 },
          { label: '80/20', pct: 80 },
          { label: '100%', pct: 100 },
        ].map((opt) => (
          <button
            key={opt.pct}
            onClick={() => setInvestPercent(opt.pct)}
            className={`flex-1 py-1.5 text-xs font-medium rounded-lg border transition-colors ${
              investPercent === opt.pct
                ? 'border-brand-500 bg-brand-500/10 text-brand-400'
                : 'border-zinc-700 text-zinc-400 hover:border-zinc-600'
            }`}
          >
            {opt.label}
          </button>
        ))}
      </div>

      {/* Defeat button */}
      <button
        onClick={handleDefeat}
        disabled={busy || amountNum <= 0}
        className="w-full py-3 bg-gradient-to-r from-amber-600 to-orange-600 hover:from-amber-500 hover:to-orange-500 disabled:opacity-40 text-white font-bold text-sm rounded-xl transition-all flex items-center justify-center gap-2 shadow-lg shadow-amber-500/10"
      >
        {busy ? (
          <Loader2 size={18} className="animate-spin" />
        ) : (
          <>
            <Shield size={18} />
            Defeat the {isBoss} Boss
          </>
        )}
      </button>

      {/* XP reward note */}
      <p className="text-center text-xs text-zinc-600 mt-2">
        +200 XP • Jess will announce your victory
      </p>
    </div>
  );
}
