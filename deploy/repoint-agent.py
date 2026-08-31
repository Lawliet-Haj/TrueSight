"""Fait pointer un agent vers une AUTRE URL de serveur, à distance.

Sert lors d'une migration de serveur : les agents gardent l'ancienne URL dans
leur ``config.ini`` et continuent donc de parler à l'ancien serveur. Tant que
celui-ci répond, on peut leur pousser une commande ordinaire qui réécrit l'URL
puis redémarre le service — aucune intervention physique.

Prérequis : l'agent doit être EN LIGNE sur le serveur d'où l'on lance ce script,
et la base du NOUVEAU serveur doit contenir son jeton (donc avoir été restaurée
depuis l'ancienne), sinon il sera rejeté en 401 après bascule.

Utilisation (depuis l'ANCIEN serveur) :
    docker cp deploy/repoint-agent.py truesight-web:/tmp/
    docker exec -w /app -e PYTHONPATH=/app truesight-web         python /tmp/repoint-agent.py <HOSTNAME> [https://nouveau-serveur]

La commande poussée est PROTECTRICE : elle sauvegarde config.ini, vérifie que la
réécriture a bien pris, et ne redémarre l'agent QUE dans ce cas. En cas de doute
elle restaure et abandonne — un agent qui parle encore à l'ancien serveur vaut
mieux qu'un poste muet.
"""

import sys
from app import create_app
from app.extensions import db
from app.models import Agent, Command, User, utcnow
from app.security import write_audit

TARGET_HOST = sys.argv[1]  # nom d'hote du poste a basculer
NEW_URL = sys.argv[2] if len(sys.argv) > 2 else "https://srv1867777.hstgr.cloud"

# Commande PROTECTRICE : on ne redemarre l'agent que si la reecriture a
# reellement abouti. En cas de doute on ne touche a rien -> l'agent continue de
# fonctionner sur l'ancien serveur, ce qui est toujours mieux qu'un poste muet.
CMD = r'''
$cfg = 'C:\ProgramData\TrueSight\config.ini'
if (-not (Test-Path $cfg)) { Write-Error 'config.ini introuvable'; exit 1 }
Copy-Item $cfg "$cfg.avant-migration" -Force
$c = Get-Content $cfg
$n = $c -replace '^\s*url\s*=.*', 'url = NEWURL'
Set-Content -Path $cfg -Value $n -Encoding ASCII
$check = (Get-Content $cfg | Select-String -Pattern '^\s*url\s*=' | Select-Object -First 1).ToString().Trim()
Write-Output ("apres reecriture -> " + $check)
if ($check -notmatch 'srv1867777') {
  Write-Error 'reecriture NON confirmee : restauration et abandon'
  Copy-Item "$cfg.avant-migration" $cfg -Force
  exit 1
}
Write-Output 'redemarrage de l agent dans 8 s (detache, pour renvoyer ce resultat)'
Start-Process powershell -WindowStyle Hidden -ArgumentList '-NoProfile','-Command','Start-Sleep 8; Restart-Service TrueSightAgent -Force'
Write-Output 'OK'
'''.replace("NEWURL", NEW_URL)

app = create_app()
with app.app_context():
    admin = db.session.query(User).filter_by(role="superadmin").first() or db.session.query(User).first()
    agent = db.session.query(Agent).filter(Agent.hostname == TARGET_HOST).first()
    if agent is None:
        raise SystemExit("poste introuvable : " + TARGET_HOST)
    cmd = Command(agent_id=agent.id, created_by=admin.id if admin else None,
                  shell="powershell", command_text=CMD, status="pending",
                  timeout_seconds=120, created_at=utcnow())
    db.session.add(cmd)
    db.session.flush()
    write_audit(action="agent.repoint", user_id=admin.id if admin else None,
                target_agent=agent.id,
                details={"command_id": str(cmd.id), "new_url": NEW_URL,
                         "via": "SSH (assistant)"}, commit=False)
    db.session.commit()
    print("COMMANDE CREEE pour %s -> %s (id=%s)" % (TARGET_HOST, NEW_URL, cmd.id))
