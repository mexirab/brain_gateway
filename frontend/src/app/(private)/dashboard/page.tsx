export default function DashboardPage() {
  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">Today</h1>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <div className="glass p-6">
          <h2 className="text-lg font-semibold text-zinc-300 mb-3">Calendar</h2>
          <p className="text-zinc-500">Coming in Phase 3</p>
        </div>
        <div className="glass p-6">
          <h2 className="text-lg font-semibold text-zinc-300 mb-3">Reminders</h2>
          <p className="text-zinc-500">Coming in Phase 3</p>
        </div>
        <div className="glass p-6">
          <h2 className="text-lg font-semibold text-zinc-300 mb-3">Focus Timer</h2>
          <p className="text-zinc-500">Coming in Phase 3</p>
        </div>
        <div className="glass p-6">
          <h2 className="text-lg font-semibold text-zinc-300 mb-3">Quick Actions</h2>
          <p className="text-zinc-500">Coming in Phase 3</p>
        </div>
      </div>
    </div>
  );
}
