import psutil
import os

class MigrationManager:
    def __init__(self):
        self.threshold_cpu = 80.0  # 80% CPU usage trigger
        self.threshold_mem = 85.0  # 85% RAM usage trigger

    def get_system_vitals(self):
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory().percent
        return {"cpu": cpu, "memory": mem}

    def draft_proposal(self, vitals):
        """
        Jarvis uses Gemini to translate technical strain into 
        a business proposal for Sir.
        """
        vitals_str = f"CPU: {vitals['cpu']}%, RAM: {vitals['memory']}%"
        
        # This string is passed to the main brain to be formatted
        return (
            f"SYSTEM ALERT: Resource usage is critical ({vitals_str}). "
            "Our current Render 'Starter' plan is bottlenecking the trading engine. "
            "I propose migrating to the 'Professional' tier to reduce latency by 40% "
            "and ensure the Kelly Criterion calculations don't lag during market spikes."
        )
