import sys
import os

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../..")))

from src.apps.comic_gen.pipeline import ComicGenPipeline
from src.apps.comic_gen.models import GenerationStatus

def test_pipeline():
    print("Initializing Pipeline...")
    pipeline = ComicGenPipeline()
    
    print("\n--- Step 1: Create Project ---")
    novel_text = "Alex entered the ancient ruins. Suddenly, Luna appeared."
    script = pipeline.create_project("The Ancient Ruins", novel_text)
    print(f"Project Created: {script.id}")
    print(f"Characters: {len(script.characters)}")
    print(f"Scenes: {len(script.scenes)}")
    print(f"Frames: {len(script.frames)}")
    
    print("\n--- Step 2: Generate Assets ---")
    script = pipeline.generate_assets(script.id)
    for char in script.characters:
        print(f"Character {char.name}: {char.status} - {char.image_url}")
        
    print("\n--- Step 3: Generate Storyboard ---")
    script = pipeline.generate_storyboard(script.id)
    for frame in script.frames:
        print(f"Frame {frame.id}: {frame.status} - {frame.image_url}")
        
    print("\n--- Step 4: Generate Video ---")
    script = pipeline.generate_video(script.id)
    for frame in script.frames:
        print(f"Frame {frame.id} Video: {frame.status} - {frame.video_url}")
        
    print("\n--- Step 5: Generate Audio ---")
    script = pipeline.generate_audio(script.id)
    for frame in script.frames:
        print(f"Frame {frame.id} Audio: {frame.audio_url}")
        print(f"Frame {frame.id} SFX: {frame.sfx_url}")
        
    print("\nPipeline Test Completed.")

if __name__ == "__main__":
    test_pipeline()
