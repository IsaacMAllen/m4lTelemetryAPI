# 1. Generate a new token
openssl rand -hex 24   # → b9e2f5a8c1d4e7b0a3f6c9e2d5b8f1a4c7d0e3b6

# 2. Update group_vars/all.yml — keep the old token briefly during rollout window
#    api_ingest_tokens: "v2_b9e2f5a8...,v1_a3f8c2d1..."

# 3. Deploy
ansible-playbook -i inventory.yml playbook.yml --tags app

# 4. Ship the new device build with @token v2_b9e2f5a8...

# 5. After users update (~1-2 weeks), remove v1 from group_vars and re-deploy
#    api_ingest_tokens: "v2_b9e2f5a8..."
