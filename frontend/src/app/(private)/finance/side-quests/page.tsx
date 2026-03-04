'use client';

import { useState } from 'react';
import { Plus, Loader2, ScrollText } from 'lucide-react';
import SideQuestCard from '@/components/finance/SideQuestCard';
import SideQuestModal from '@/components/finance/SideQuestModal';
import XPToast from '@/components/finance/XPToast';
import { useFinance } from '@/lib/finance-context';
import { financeApi } from '@/lib/finance-api';

export default function SideQuestsPage() {
  const { sideQuests, loading, error, refresh, awardXP, lastXPGain, clearXPGain } =
    useFinance();
  const [showModal, setShowModal] = useState(false);

  const active = sideQuests.filter((q) => q.status === 'active');
  const completed = sideQuests.filter((q) => q.status === 'completed');
  const abandoned = sideQuests.filter((q) => q.status === 'abandoned');

  async function handleCreate(quest: {
    name: string;
    target_amount: number;
    monthly_carve: number;
    description?: string;
    icon?: string;
  }) {
    await financeApi.createSideQuest(quest);
    await refresh();
  }

  async function handleContribute(questId: number, amount: number) {
    const result = await financeApi.contributeSideQuest(questId, amount);
    if (result.status === 'completed') {
      await awardXP('side_quest_complete', `Completed: ${result.name}`);
      try {
        await financeApi.announce(`Side quest complete! You unlocked ${result.name}. Guilt free!`);
      } catch {
        // TTS not critical
      }
    }
    await refresh();
  }

  async function handleComplete(questId: number) {
    const result = await financeApi.completeSideQuest(questId);
    await awardXP('side_quest_complete', `Completed: ${result.name}`);
    try {
      await financeApi.announce(`Side quest complete! You unlocked ${result.name}. Guilt free!`);
    } catch {
      // TTS not critical
    }
    await refresh();
  }

  async function handleAbandon(questId: number) {
    await financeApi.abandonSideQuest(questId);
    await refresh();
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-[60vh]">
        <Loader2 className="animate-spin text-brand-500" size={32} />
      </div>
    );
  }

  if (error) {
    return (
      <div className="max-w-4xl mx-auto mt-12">
        <div className="glass p-6 border border-red-500/30 text-center">
          <p className="text-red-400 font-semibold">Failed to load side quests</p>
          <p className="text-sm text-zinc-500 mt-1">{error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto space-y-6">
      {/* XP Toast */}
      {lastXPGain && (
        <XPToast
          amount={lastXPGain.amount}
          description={lastXPGain.description}
          onDismiss={clearXPGain}
        />
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-zinc-100">Side Quests</h1>
          <p className="text-sm text-zinc-500 mt-0.5">
            Save toward big purchases guilt-free
          </p>
        </div>
        <button
          onClick={() => setShowModal(true)}
          className="flex items-center gap-1.5 px-4 py-2 bg-brand-600 hover:bg-brand-700 text-white text-sm font-medium rounded-lg transition-colors"
        >
          <Plus size={16} />
          New Quest
        </button>
      </div>

      {/* Active Quests */}
      {active.length > 0 ? (
        <div className="space-y-4">
          <h2 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider">
            Active ({active.length})
          </h2>
          {active.map((quest) => (
            <SideQuestCard
              key={quest.id}
              quest={quest}
              onContribute={handleContribute}
              onComplete={handleComplete}
              onAbandon={handleAbandon}
            />
          ))}
        </div>
      ) : (
        <div className="glass p-8 text-center">
          <ScrollText size={32} className="text-zinc-600 mx-auto mb-3" />
          <p className="text-zinc-400 text-sm">No active quests</p>
          <p className="text-zinc-600 text-xs mt-1">
            Create a side quest to start saving toward something special
          </p>
        </div>
      )}

      {/* Completed Quests */}
      {completed.length > 0 && (
        <div className="space-y-4">
          <h2 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider">
            Completed ({completed.length})
          </h2>
          {completed.map((quest) => (
            <SideQuestCard
              key={quest.id}
              quest={quest}
              onContribute={handleContribute}
              onComplete={handleComplete}
              onAbandon={handleAbandon}
            />
          ))}
        </div>
      )}

      {/* Abandoned Quests */}
      {abandoned.length > 0 && (
        <div className="space-y-4">
          <h2 className="text-xs font-semibold text-zinc-500 uppercase tracking-wider">
            Abandoned ({abandoned.length})
          </h2>
          {abandoned.map((quest) => (
            <SideQuestCard
              key={quest.id}
              quest={quest}
              onContribute={handleContribute}
              onComplete={handleComplete}
              onAbandon={handleAbandon}
            />
          ))}
        </div>
      )}

      {/* Modal */}
      {showModal && (
        <SideQuestModal
          onClose={() => setShowModal(false)}
          onCreate={handleCreate}
        />
      )}
    </div>
  );
}
