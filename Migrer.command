#!/bin/zsh
cd -- "${0:A:h}" || exit 1
echo 'Arc → Zen — remplacement du profil Zen par défaut, avec sauvegarde.'
if command -v python3 >/dev/null 2>&1; then
  python3 migrate.py --apply
  result=$?
else
  echo 'Python 3 est nécessaire. Installe-le puis relance ce fichier.'
  result=1
fi
echo
read -r '?Appuie sur Entrée pour fermer cette fenêtre.'
exit "$result"
