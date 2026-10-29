import git
import os
import shutil

class EvolutionProtocol:
    def __init__(self):
        # The GITHUB_TOKEN and REPO_URL must be in your Render Environment Variables
        # Format: https://<token>@github.com/username/repo.git
        self.repo_url = os.environ.get("REPO_URL")
        self.repo_path = os.getcwd()

    def update_dna(self, file_name, new_content, change_summary="System Self-Update"):
        """
        Rewrites a specific file and pushes the change to GitHub.
        """
        try:
            target_file = os.path.join(self.repo_path, "app", file_name)
            
            # 1. Physical file rewrite
            with open(target_file, "w") as f:
                f.write(new_content)
            
            # 2. Git Operations
            repo = git.Repo(self.repo_path)
            
            # Ensure we are on the main branch
            repo.git.checkout('main')
            
            # Stage, Commit, and Push
            repo.index.add([target_file])
            repo.index.commit(f"JARVIS EVOLUTION: {change_summary}")
            
            origin = repo.remote(name='origin')
            origin.push()
            
            return True, f"Evolution successful, Sir. Changes to {file_name} are live on GitHub."
        
        except Exception as e:
            return False, f"Evolution failed, Sir. Diagnostic: {str(e)}"

    def self_heal(self, error_traceback):
        """
        A placeholder for Phase 3 logic where Jarvis 
        analyzes an error and calls update_dna on himself.
        """
        pass
